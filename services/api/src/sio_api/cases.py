"""Tenant-scoped incident casework with authoritative, immutable evidence snapshots."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import ValidationError

from sio_core.authn import Principal
from sio_core.authz import authorise
from sio_core.guard import principal_of
from sio_core.tenancy import current_tenant
from sio_schemas import new_id

from .case_evidence import (
    case_evidence_items,
    recorded_evidence,
    resolve_frames,
    source_evidence_zones,
    source_identity,
)
from .case_helpers import (
    CASE_SOURCE_IDS,
    CaseCreate,
    CasePatch,
    EvidenceAttach,
    MissionLink,
    NoteCreate,
    SavedSearchCreate,
    SearchQuery,
    case_metrics,
    escape_like,
    iso_now,
    matches_result,
    printable_report,
)
from .evaluation_metrics import Annotation, fingerprint
from .video_jobs import video_mutation_lock
from .workbench_store import WorkbenchConflict, WorkbenchStore

SCAN_LIMIT = 5000
MAX_NOTES = 500
MAX_EVIDENCE_ITEMS = 20


def review_video_available(video: dict[str, Any] | None) -> bool:
    """A retained database row is not proof that its media remains in the active library."""
    if not video:
        return False
    return (
        not any(video.get(key) for key in ("archived_at", "purged_at", "purge_started_at"))
        and video.get("status")
        not in {"archived", "purging", "purge_failed", "purged", "unavailable", "missing"}
        and video.get("media_available") is not False
    )


def actor(request: Request) -> Principal:
    principal = principal_of(request)
    if (
        not principal.subject
        or principal.subject == "anonymous"
        or principal.tenant_id != current_tenant()
    ):
        raise HTTPException(401, "An authenticated tenant identity is required")
    return principal


def allowed(principal: Principal, action: str, zone_id: str | None = None) -> bool:
    return (
        principal.may_see_zone(zone_id)
        and authorise(principal, action, context={"zone_id": zone_id}).allowed
    )


def need(principal: Principal, action: str, zone_id: str | None = None) -> None:
    if not allowed(principal, action, zone_id):
        raise HTTPException(403, "Your role or zone access does not permit this record")


def case_visible(principal: Principal, record: dict[str, Any]) -> bool:
    if not allowed(principal, "case.read", record.get("zone_id")):
        return False
    for item in [record, *(record.get("evidence_attachments") or [])]:
        zones = source_evidence_zones(item)
        evidence = item.get("evidence") or {}
        actions = ["case.read"]
        if recorded_evidence(item):
            actions.extend(["review.read", "media.read"])
        elif evidence.get("resolved_frames"):
            actions.append("media.read")
        scope_zones: list[str | None] = list(zones) or [None]
        if any(not allowed(principal, action, zone) for action in actions for zone in scope_zones):
            return False
    return True


def conflict() -> HTTPException:
    return HTTPException(409, "This case changed. Refresh it before saving your changes.")


def audit_entry(
    kind: str, principal: Principal, summary: str, ref: str | None = None
) -> dict[str, Any]:
    return {
        "kind": kind,
        "ts": iso_now(),
        "author": principal.subject,
        "summary": summary,
        "ref": ref,
    }


def source_case_id(body: CaseCreate) -> str:
    source = ":".join(source_identity(body.model_dump()) or ())
    return "case_" + hashlib.sha256(source.encode()).hexdigest()[:24]


def latest_resolution_note(record: dict[str, Any]) -> str | None:
    """Expose old case records without rewriting their permanent note history on reads."""
    if record.get("resolution_note"):
        return str(record["resolution_note"])
    return next(
        (
            str(note["text"])
            for note in reversed(record.get("notes", []))
            if note.get("kind") == "resolution" and note.get("text")
        ),
        None,
    )


class Casework:
    def __init__(self, store: WorkbenchStore, pool: Any) -> None:
        self.store = store
        self.pool = pool

    async def load(self, case_id: str, principal: Principal) -> dict[str, Any]:
        record = await self.store.get(current_tenant(), "case", case_id)
        if record is None:
            raise HTTPException(404, "Case not found")
        if not case_visible(principal, record):
            raise HTTPException(
                403, "Your role or zone access does not permit all evidence in this case"
            )
        return record

    async def source(self, body: CaseCreate, principal: Principal) -> dict[str, Any]:
        tenant = current_tenant()
        captured = iso_now()
        if body.alert_id:
            need(principal, "alerts.read")
            row = await self.pool.fetchrow(
                "SELECT payload FROM alerts WHERE tenant_id = %s AND alert_id = %s",
                (tenant, body.alert_id),
            )
            if not row:
                raise HTTPException(404, "Alert not found")
            alert = deepcopy(dict(row["payload"]))
            need(principal, "alerts.read", alert.get("zone_id"))
            events: list[dict[str, Any]] = []
            event_ids = list(alert.get("event_ids") or [])[:500]
            if event_ids:
                rows = await self.pool.fetch(
                    "SELECT payload FROM events WHERE tenant_id = %s AND event_id = ANY(%s) ORDER BY ts",
                    (tenant, event_ids),
                )
                events = [deepcopy(dict(item["payload"])) for item in rows]
                for event in events:
                    need(principal, "events.read", event.get("zone_id"))
            frame_evidence = (
                await resolve_frames(self.pool, tenant, alert, events)
                if allowed(principal, "media.read", alert.get("zone_id"))
                else {
                    "resolved_frames": [],
                    "unresolved_media": [],
                    "media_note": "Media access is restricted for this role.",
                }
            )
            evidence_zones = sorted(
                {str(item["zone_id"]) for item in [alert, *events] if item.get("zone_id")}
            )
            return {
                "title": alert.get("title", "Platform incident"),
                "zone_id": alert.get("zone_id"),
                "source_id": None,
                "at_s": None,
                "evidence_zone_ids": evidence_zones,
                "evidence": {
                    "source": "platform_alert",
                    **frame_evidence,
                    "alert": alert,
                    "events": events,
                    "captured_at": captured,
                    "missing_event_ids": [
                        item
                        for item in event_ids
                        if item not in {event.get("event_id") for event in events}
                    ],
                    "provenance_note": "Events preserve recorded rule IDs and evidence. Rule/model versions not recorded by the original pipeline remain unknown.",
                },
            }
        need(principal, "review.read")
        video = await self.store.get(tenant, "video", str(body.video_id))
        analysis = await self.store.get(tenant, "analysis", str(body.analysis_id))
        if not video or not analysis or analysis.get("video_id") != body.video_id:
            raise HTTPException(404, "Video analysis not found")
        if not review_video_available(video):
            raise HTTPException(409, "Recorded footage is unavailable for a new case")
        if analysis.get("status") != "completed":
            raise HTTPException(409, "Wait for a completed analysis before opening a case")
        if body.annotation_set_id:
            return await self.annotation_source(body, principal, video, analysis, captured)
        video_event = next(
            (
                event
                for event in analysis.get("events", [])
                if event.get("event_id") == body.event_id
            ),
            None,
        )
        if (
            not video_event
            or video_event.get("video_id", body.video_id) != body.video_id
            or video_event.get("analysis_id", body.analysis_id) != body.analysis_id
        ):
            raise HTTPException(404, "Event not found in this video analysis")
        need(principal, "review.read", video_event.get("zone_id"))
        snapshot = {
            "title": video_event.get("title", "Recorded-video incident"),
            "zone_id": video_event.get("zone_id"),
            "source_id": body.video_id,
            "at_s": video_event.get("at_s"),
            "evidence_zone_ids": [video_event["zone_id"]] if video_event.get("zone_id") else [],
            "evidence": {
                "source": "recorded_file",
                "event": deepcopy(video_event),
                "video": {
                    key: video.get(key)
                    for key in ("video_id", "title", "duration_s", "privacy", "source")
                },
                "analysis": {
                    key: analysis.get(key)
                    for key in ("analysis_id", "status", "created_at", "updated_at")
                },
                "model": deepcopy(analysis.get("model")),
                "rules": deepcopy(analysis.get("rules", [])),
                "zones": deepcopy(analysis.get("zones", [])),
                "captured_at": captured,
            },
        }
        snapshot["evidence_zone_ids"] = sorted(source_evidence_zones(snapshot))
        if not case_visible(principal, snapshot):
            raise HTTPException(
                403, "Your access does not cover every zone in the retained analysis snapshot"
            )
        return snapshot

    async def annotation_source(
        self,
        body: CaseCreate,
        principal: Principal,
        video: dict[str, Any],
        analysis: dict[str, Any],
        captured: str,
    ) -> dict[str, Any]:
        tenant = current_tenant()
        frozen = await self.store.get(tenant, "evaluation_annotations", str(body.annotation_set_id))
        if (
            not frozen
            or frozen.get("video_id") != body.video_id
            or frozen.get("annotation_set_id") != body.annotation_set_id
        ):
            raise HTTPException(404, "Frozen annotation set not found for this recording")
        for zone in source_evidence_zones({"evidence": frozen}):
            need(principal, "review.read", zone)
        if not all(frozen.get(key) for key in ("frozen_by", "frozen_at", "annotation_hash")):
            raise HTTPException(422, "A frozen reviewer annotation version is required")
        content = {
            key: value
            for key, value in frozen.items()
            if key
            not in {
                "record_id",
                "revision",
                "created_at",
                "updated_at",
                "annotation_set_id",
                "annotation_hash",
                "draft_revision",
                "frozen_by",
                "frozen_at",
            }
        }
        if fingerprint(content) != frozen["annotation_hash"]:
            raise HTTPException(
                422, "The frozen annotation content does not match its recorded hash"
            )
        annotation = next(
            (
                row
                for row in frozen.get("annotations", [])
                if row.get("annotation_id") == body.annotation_id
            ),
            None,
        )
        if annotation is None:
            raise HTTPException(404, "Annotation not found in this frozen set")
        try:
            label = Annotation.model_validate(annotation)
        except ValidationError as error:
            raise HTTPException(
                422, "The frozen annotation interval or label is invalid"
            ) from error
        if label.end_s > float(video.get("duration_s") or 0):
            raise HTTPException(422, "The frozen annotation exceeds the retained recording")
        evidence = {
            "source": "reviewer_annotation",
            "annotation": deepcopy(annotation),
            "annotation_set": deepcopy(frozen),
            "video": {
                key: video.get(key)
                for key in ("video_id", "title", "duration_s", "privacy", "source")
            },
            "analysis": {
                key: analysis.get(key)
                for key in ("analysis_id", "status", "created_at", "updated_at")
            },
            "model": deepcopy(analysis.get("model")),
            "rules": deepcopy(analysis.get("rules", [])),
            "zones": deepcopy(analysis.get("zones", [])),
            "captured_at": captured,
            "provenance_note": "Reviewer-authored incident window from a frozen annotation set. No detector event or confidence is implied. Creating or reviewing this case does not change evaluation results.",
        }
        if body.evaluation_report_id:
            report = await self.store.get(tenant, "evaluation_report", body.evaluation_report_id)
            if not report:
                raise HTTPException(404, "Evaluation report not found")
            for zone in source_evidence_zones({"evidence": report}):
                need(principal, "review.read", zone)
            report_input = next(
                (
                    row
                    for row in report.get("inputs", [])
                    if row.get("video_id") == body.video_id
                    and row.get("annotation_set_id") == body.annotation_set_id
                    and body.analysis_id in row.get("analysis_ids", [])
                ),
                None,
            )
            report_set = next(
                (
                    row
                    for row in report.get("annotation_snapshots", [])
                    if row.get("video_id") == body.video_id
                    and row.get("annotation_set_id") == body.annotation_set_id
                ),
                None,
            )
            match = next(
                (
                    (candidate, clip, miss)
                    for candidate in report.get("candidates", [])
                    for clip in candidate.get("clips", [])
                    if clip.get("video_id") == body.video_id
                    and clip.get("analysis_id") == body.analysis_id
                    and clip.get("annotation_set_id") == body.annotation_set_id
                    for miss in clip.get("misses", [])
                    if miss.get("annotation_id") == body.annotation_id
                ),
                None,
            )
            if (
                not report_input
                or not report_set
                or report_set.get("annotation_hash") != frozen["annotation_hash"]
                or annotation not in report_set.get("annotations", [])
                or not match
                or match[2] != annotation
                or match[1].get("provenance", {}).get("annotation_hash")
                != frozen["annotation_hash"]
            ):
                raise HTTPException(
                    422,
                    "This report does not contain the exact annotation as a miss for this recording and analysis",
                )
            candidate, clip, miss = match
            evidence["evaluation_report"] = {
                **{
                    key: deepcopy(report.get(key))
                    for key in (
                        "report_id",
                        "title",
                        "created_at",
                        "created_by",
                        "revision",
                        "partition",
                        "tolerance_s",
                        "matching_version",
                        "limitations",
                    )
                },
                "input": deepcopy(report_input),
                "candidate": candidate.get("candidate"),
                "miss": deepcopy(miss),
                "clip": deepcopy(clip),
            }
        snapshot: dict[str, Any] = {
            "title": f"Reviewer {label.event_type} annotation · {video.get('title') or body.video_id}",
            "zone_id": label.zone_id,
            "source_id": body.video_id,
            "at_s": label.start_s,
            "evidence": evidence,
        }
        snapshot["evidence_zone_ids"] = sorted(source_evidence_zones(snapshot))
        if not case_visible(principal, snapshot):
            raise HTTPException(
                403, "Your access does not cover every zone in the frozen reviewer evidence"
            )
        return snapshot

    async def create(self, body: CaseCreate, principal: Principal) -> dict[str, Any]:
        need(principal, "case.write")
        if body.video_id:
            async with video_mutation_lock(self.store, current_tenant(), body.video_id):
                video = await self.store.get(current_tenant(), "video", body.video_id)
                if not video or video.get("video_id") != body.video_id:
                    raise HTTPException(404, "Recorded footage not found")
                if not review_video_available(video):
                    raise HTTPException(409, "Recorded footage is unavailable for a new case")
                return await self._create_locked(body, principal)
        return await self._create_locked(body, principal)

    async def _create_locked(self, body: CaseCreate, principal: Principal) -> dict[str, Any]:
        need(principal, "case.write")
        source = await self.source(body, principal)
        if not case_visible(principal, source):
            raise HTTPException(403, "Your access does not cover every zone in the source evidence")
        case_id = source_case_id(body)
        existing = await self.store.get(current_tenant(), "case", case_id)
        if existing is not None:
            return await self.load(case_id, principal)
        record = {
            **source,
            **body.model_dump(mode="json"),
            "title": body.title or source["title"],
            "case_id": case_id,
            "status": "open",
            "verdict": "unreviewed",
            "created_by": principal.subject,
            "mission_ids": [],
            "notes": [],
            "evidence_attachments": [],
            "first_response_at": None,
            "resolved_at": None,
            "resolution_note": None,
            "timeline": [
                audit_entry(
                    "created", principal, "Case opened with an authoritative evidence snapshot"
                )
            ],
        }
        try:
            return await self.store.put(
                current_tenant(), "case", case_id, record, expected_revision=0
            )
        except WorkbenchConflict:
            return await self.load(case_id, principal)

    async def attach(
        self, case_id: str, body: EvidenceAttach, principal: Principal
    ) -> dict[str, Any]:
        need(principal, "case.write")
        if body.video_id:
            async with video_mutation_lock(self.store, current_tenant(), body.video_id):
                return await self._attach_locked(case_id, body, principal)
        return await self._attach_locked(case_id, body, principal)

    async def _attach_locked(
        self, case_id: str, body: EvidenceAttach, principal: Principal
    ) -> dict[str, Any]:
        record = deepcopy(await self.load(case_id, principal))
        need(principal, "case.write", record.get("zone_id"))
        request = body.source_request()
        identity = source_identity(request.model_dump())
        if any(source_identity(item) == identity for item in case_evidence_items(record)):
            if request.evaluation_report_id:
                await self.source(request, principal)
            return await self.detail(case_id, principal)
        if record["revision"] != body.expected_revision:
            raise conflict()
        if len(record.get("evidence_attachments") or []) >= MAX_EVIDENCE_ITEMS - 1:
            raise HTTPException(
                409, "A case supports at most 20 evidence sources, including its original"
            )
        snapshot = await self.source(request, principal)
        attachment_id = "evidence_" + source_case_id(request).removeprefix("case_")
        item = {
            **snapshot,
            **request.model_dump(include=CASE_SOURCE_IDS),
            "attachment_id": attachment_id,
            "attached_by": principal.subject,
            "attached_at": iso_now(),
            "note": body.note,
        }
        record.setdefault("evidence_attachments", []).append(item)
        if not case_visible(principal, record):
            raise HTTPException(
                403, "Your role or zone access does not permit all attached evidence"
            )
        record.setdefault("timeline", []).append(
            audit_entry(
                "evidence_attached",
                principal,
                f"Attached evidence: {snapshot['title']}",
                attachment_id,
            )
        )
        record["first_response_at"] = record.get("first_response_at") or item["attached_at"]
        try:
            await self.store.put(
                current_tenant(), "case", case_id, record, expected_revision=body.expected_revision
            )
        except WorkbenchConflict as error:
            latest = await self.load(case_id, principal)
            if any(source_identity(item) == identity for item in case_evidence_items(latest)):
                return await self.detail(case_id, principal)
            raise conflict() from error
        return await self.detail(case_id, principal)

    async def patch(self, case_id: str, body: CasePatch, principal: Principal) -> dict[str, Any]:
        record = deepcopy(await self.load(case_id, principal))
        need(principal, "case.write", record.get("zone_id"))
        if record["revision"] != body.expected_revision:
            raise conflict()
        changes = body.model_dump(
            mode="json", exclude_unset=True, exclude={"expected_revision", "resolution_note"}
        )
        now = iso_now()
        resolution_note = body.resolution_note
        previous_resolution = latest_resolution_note(record)
        newly_resolved = changes.get("status") == "resolved" and record["status"] != "resolved"
        if newly_resolved:
            if not resolution_note:
                raise HTTPException(422, "Add a resolution note before resolving the case")
            record["resolved_at"] = now
        elif changes.get("status") in ("open", "investigating") and record["status"] == "resolved":
            record["resolved_at"] = None
        if (
            changes.get("status") == "investigating"
            or changes.get("verdict") in ("confirmed", "false_positive")
            or resolution_note
        ):
            record["first_response_at"] = record.get("first_response_at") or now
        record.update(changes)
        if resolution_note:
            record["resolution_note"] = resolution_note
            # The form resends its populated resolution on ordinary owner/title edits. Only an
            # actual reason change or a new resolution creates another permanent authored note.
            if resolution_note != previous_resolution or newly_resolved:
                self.append_note(record, resolution_note, principal, kind="resolution")
                changes["resolution_note"] = resolution_note
        elif previous_resolution:
            record["resolution_note"] = previous_resolution
        # Exact changed fields are retained without client-controlled authorship or timestamps.
        entry = audit_entry(
            "updated",
            principal,
            "Updated " + ", ".join(changes) if changes else "Saved case review",
        )
        entry["changes"] = changes
        record.setdefault("timeline", []).append(entry)
        try:
            return await self.store.put(
                current_tenant(), "case", case_id, record, expected_revision=body.expected_revision
            )
        except WorkbenchConflict as exc:
            raise conflict() from exc

    @staticmethod
    def append_note(
        record: dict[str, Any], text: str, principal: Principal, *, kind: str = "note"
    ) -> dict[str, Any]:
        if len(record.get("notes", [])) >= MAX_NOTES:
            raise HTTPException(
                409,
                f"This case has reached its {MAX_NOTES}-note limit; export it before opening a continuation case",
            )
        note = {
            "note_id": new_id("note"),
            "text": text,
            "author": principal.subject,
            "created_at": iso_now(),
            "kind": kind,
        }
        record.setdefault("notes", []).append(note)
        record.setdefault("timeline", []).append(
            audit_entry("note", principal, "Added " + kind, note["note_id"])
        )
        record["first_response_at"] = record.get("first_response_at") or note["created_at"]
        return note

    async def note(self, case_id: str, body: NoteCreate, principal: Principal) -> dict[str, Any]:
        for _ in range(5):
            record = deepcopy(await self.load(case_id, principal))
            need(principal, "case.write", record.get("zone_id"))
            note = self.append_note(record, body.text, principal)
            try:
                await self.store.put(
                    current_tenant(), "case", case_id, record, expected_revision=record["revision"]
                )
                return note
            except WorkbenchConflict:
                continue
        raise conflict()

    async def mission(
        self, case_id: str, body: MissionLink, principal: Principal
    ) -> dict[str, Any]:
        record = deepcopy(await self.load(case_id, principal))
        need(principal, "case.write", record.get("zone_id"))
        if record["revision"] != body.expected_revision:
            raise conflict()
        row = await self.pool.fetchrow(
            "SELECT mission_id, zone_id FROM missions WHERE tenant_id = %s AND mission_id = %s",
            (current_tenant(), body.mission_id),
        )
        if not row:
            raise HTTPException(404, "Mission not found")
        need(principal, "mission.read", row.get("zone_id"))
        if body.mission_id in record.get("mission_ids", []):
            return record
        record.setdefault("mission_ids", []).append(body.mission_id)
        record.setdefault("timeline", []).append(
            audit_entry(
                "mission_linked",
                principal,
                "Linked an existing mission; its state is unchanged",
                body.mission_id,
            )
        )
        record["first_response_at"] = record.get("first_response_at") or iso_now()
        try:
            return await self.store.put(
                current_tenant(), "case", case_id, record, expected_revision=body.expected_revision
            )
        except WorkbenchConflict as exc:
            raise conflict() from exc

    async def detail(self, case_id: str, principal: Principal) -> dict[str, Any]:
        record = deepcopy(await self.load(case_id, principal))
        record["evidence_items"] = case_evidence_items(record)
        record["evidence_count"] = len(record["evidence_items"])
        record["recorded_evidence_count"] = sum(
            recorded_evidence(item) for item in record["evidence_items"]
        )
        record["resolution_note"] = latest_resolution_note(record)
        tenant = current_tenant()
        missions = []
        if record.get("mission_ids") and allowed(principal, "mission.read"):
            rows = await self.pool.fetch(
                "SELECT mission_id, name, description, state, commander, zone_id, assignees, resources, created_ts, updated_ts, started_ts, completed_ts, payload FROM missions WHERE tenant_id = %s AND mission_id = ANY(%s)",
                (tenant, record["mission_ids"]),
            )
            missions = [
                dict(row) for row in rows if allowed(principal, "mission.read", row.get("zone_id"))
            ]
        decisions = []
        alerts = [
            (item.get("evidence") or {}).get("alert") or {} for item in record["evidence_items"]
        ]
        decision_ids = list(
            {identity for alert in alerts for identity in alert.get("decision_ids", [])}
        )
        # Decisions produced after case opening also belong to the exact trigger-event set.
        event_ids = list({identity for alert in alerts for identity in alert.get("event_ids", [])})
        if (decision_ids or event_ids) and allowed(principal, "decisions.read"):
            rows = await self.pool.fetch(
                "SELECT payload FROM decisions WHERE tenant_id = %s AND (decision_id = ANY(%s) OR trigger_event = ANY(%s)) ORDER BY ts",
                (tenant, decision_ids, event_ids),
            )
            decisions = [dict(row["payload"]) for row in rows]
        record["missions"] = jsonable_encoder(missions)
        record["decisions"] = jsonable_encoder(decisions)
        record["mission_ids"] = [row["mission_id"] for row in missions]
        return record

    async def visible_cases(self, principal: Principal) -> list[dict[str, Any]]:
        rows = await self.store.list(current_tenant(), "case", limit=SCAN_LIMIT)
        return [row for row in rows if case_visible(principal, row)]

    async def search(self, query: SearchQuery, principal: Principal) -> dict[str, Any]:
        tenant = current_tenant()
        results: list[dict[str, Any]] = []
        video_availability: dict[str, bool] = {}
        database_kinds = {
            "entity": (
                "entities",
                "entity_id",
                "last_seen",
                "COALESCE(label, type)",
                "entities.read",
            ),
            "event": ("events", "event_id", "ts", "type", "events.read"),
            "alert": ("alerts", "alert_id", "ts", "title", "alerts.read"),
        }
        for kind, (table, id_col, ts_col, title_col, permission) in database_kinds.items():
            if query.kind not in (None, kind) or not allowed(principal, permission):
                continue
            clauses = ["tenant_id = %s"]
            params: list[Any] = [tenant]
            if query.q:
                clauses.append(
                    f"({id_col} ILIKE %s ESCAPE '\\' OR {title_col} ILIKE %s ESCAPE '\\')"
                )
                params.extend([f"%{escape_like(query.q)}%"] * 2)
            if query.zone_id:
                clauses.append("zone_id = %s")
                params.append(query.zone_id)
            if principal.zones and not principal.is_admin:
                clauses.append("(zone_id IS NULL OR zone_id = ANY(%s))")
                params.append(sorted(principal.zones))
            if query.source_id:
                clauses.append(
                    "(payload->>'source_id' = %s OR COALESCE(payload->'source_ids', '[]'::jsonb) ? %s)"
                )
                params.extend([query.source_id, query.source_id])
            for operator, date in ((">=", query.since), ("<=", query.until)):
                if date:
                    clauses.append(f"{ts_col} {operator} %s")
                    params.append(date)
            params.append(query.limit)
            rows = await self.pool.fetch(
                f"SELECT {id_col} AS id, {ts_col} AS ts, {title_col} AS title, zone_id, payload FROM {table} WHERE {' AND '.join(clauses)} ORDER BY {ts_col} DESC LIMIT %s",
                params,
            )
            for row in rows:
                if not allowed(principal, permission, row.get("zone_id")):
                    continue
                payload = row.get("payload") or {}
                results.append(
                    {
                        "kind": kind,
                        "id": row["id"],
                        "title": row["title"],
                        "ts": jsonable_encoder(row["ts"]),
                        "zone_id": row.get("zone_id"),
                        "source_id": payload.get("source_id"),
                        "source_ids": payload.get("source_ids", []),
                        "summary": (payload.get("explanation") or {}).get("summary")
                        or payload.get("type", ""),
                    }
                )
        for kind in ("video", "case", "site", "analysis"):
            result_kind = "video_event" if kind == "analysis" else kind
            if query.kind not in (None, result_kind):
                continue
            if kind == "case":
                records = await self.visible_cases(principal)
            else:
                records = await self.store.list(tenant, kind, limit=500)
            for record in records:
                if kind == "video":
                    video_id = record.get("video_id", record.get("record_id"))
                    if not isinstance(video_id, str) or not video_id:
                        continue
                    video_availability[video_id] = review_video_available(record)
                    if not video_availability[video_id]:
                        continue
                if kind == "analysis":
                    if record.get("status") != "completed":
                        continue
                    video_id = record.get("video_id")
                    if not video_id:
                        continue
                    if video_id not in video_availability:
                        video_availability[video_id] = review_video_available(
                            await self.store.get(tenant, "video", video_id)
                        )
                    if not video_availability[video_id]:
                        continue
                    candidates = [
                        {
                            "kind": "video_event",
                            "id": event["event_id"],
                            "title": event.get("title", "Video event"),
                            "ts": record.get("created_at"),
                            "source_id": record.get("video_id"),
                            "video_id": record.get("video_id"),
                            "analysis_id": record.get("analysis_id", record.get("record_id")),
                            "at_s": event.get("at_s"),
                            "zone_id": event.get("zone_id"),
                            "summary": f"Recorded footage at {event.get('at_s', 0):.1f}s",
                        }
                        for event in record.get("events", [])
                    ]
                else:
                    candidates = [
                        {
                            "kind": kind,
                            "id": record.get(f"{kind}_id", record.get("record_id")),
                            "title": record.get("title", record.get("name", "Untitled")),
                            "ts": record.get("created_at"),
                            "source_id": record.get("source_id", record.get("video_id")),
                            "video_id": record.get("video_id"),
                            "analysis_id": record.get("analysis_id"),
                            "case_id": record.get("case_id"),
                            "at_s": record.get("at_s"),
                            "zone_id": record.get("zone_id"),
                            "summary": record.get("summary", record.get("description", "")),
                        }
                    ]
                results.extend(
                    item
                    for item in candidates
                    if principal.may_see_zone(item.get("zone_id")) and matches_result(item, query)
                )
        results.sort(key=lambda item: str(item.get("ts") or ""), reverse=True)
        return {
            "results": results[: query.limit],
            "count": min(len(results), query.limit),
            "limit": query.limit,
            "possibly_truncated": len(results) >= query.limit,
            "search_mode": "literal_ids_and_labels",
            "note": "Video-event dates are analysis creation dates; at_s is time within footage. Workbench searches scan the latest 500 projects or analyses and 5000 cases.",
        }


def install_case_routes(app: FastAPI, settings: Any, store: WorkbenchStore, pool: Any) -> None:
    casework = Casework(store, pool)

    @app.get("/api/cases", tags=["casework"])
    async def cases(
        request: Request,
        status: Literal["open", "investigating", "resolved"] | None = None,
        verdict: Literal["unreviewed", "confirmed", "false_positive"] | None = None,
        owner: str | None = None,
        q: str = Query(default="", max_length=200),
    ) -> dict[str, Any]:
        principal = actor(request)
        rows = await casework.visible_cases(principal)
        filtered = [
            row
            for row in rows
            if (not status or row["status"] == status)
            and (not verdict or row["verdict"] == verdict)
            and (not owner or row.get("owner") == owner)
            and (
                not q
                or q.casefold()
                in f"{row['case_id']} {row['title']} {row.get('summary', '')}".casefold()
            )
        ]
        # Evidence and notes remain in the detail endpoint; list rows stay small and avoid stale edits.
        summaries = [
            {
                key: value
                for key, value in row.items()
                if key
                not in ("evidence", "timeline", "notes", "evidence_attachments", "evidence_items")
            }
            | {
                "evidence_count": len(case_evidence_items(row)),
                "recorded_evidence_count": sum(
                    recorded_evidence(item) for item in case_evidence_items(row)
                ),
            }
            for row in filtered
        ]
        return {
            "cases": summaries,
            "count": len(summaries),
            "scan_limit": SCAN_LIMIT,
            "possibly_truncated": len(rows) >= SCAN_LIMIT,
        }

    @app.post("/api/cases", tags=["casework"])
    async def create_case(body: CaseCreate, request: Request) -> dict[str, Any]:
        return await casework.create(body, actor(request))

    @app.get("/api/cases/{case_id}", tags=["casework"])
    async def get_case(case_id: str, request: Request) -> dict[str, Any]:
        return await casework.detail(case_id, actor(request))

    @app.patch("/api/cases/{case_id}", tags=["casework"])
    async def update_case(case_id: str, body: CasePatch, request: Request) -> dict[str, Any]:
        return await casework.patch(case_id, body, actor(request))

    @app.post("/api/cases/{case_id}/notes", tags=["casework"])
    async def add_note(case_id: str, body: NoteCreate, request: Request) -> dict[str, Any]:
        return await casework.note(case_id, body, actor(request))

    @app.post("/api/cases/{case_id}/evidence", tags=["casework"])
    async def attach_evidence(
        case_id: str, body: EvidenceAttach, request: Request
    ) -> dict[str, Any]:
        return await casework.attach(case_id, body, actor(request))

    @app.post("/api/cases/{case_id}/missions", tags=["casework"])
    async def link_mission(case_id: str, body: MissionLink, request: Request) -> dict[str, Any]:
        return await casework.mission(case_id, body, actor(request))

    @app.get("/api/cases/{case_id}/export", tags=["casework"])
    async def export_case(
        case_id: str, request: Request, format: Literal["json", "html"] = "json"
    ) -> Response:
        principal = actor(request)
        detail = await casework.detail(case_id, principal)
        report = {
            "format_version": 1,
            "exported_at": iso_now(),
            "exported_by": principal.subject,
            "case": {
                key: value for key, value in detail.items() if key not in ("missions", "decisions")
            },
            "missions": detail["missions"],
            "decisions": detail["decisions"],
        }
        headers = {
            "Content-Disposition": f'attachment; filename="{source_safe_filename(case_id)}.{format}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
        return (
            HTMLResponse(printable_report(report), headers=headers)
            if format == "html"
            else JSONResponse(jsonable_encoder(report), headers=headers)
        )

    @app.get("/api/review/search", tags=["casework"])
    async def search(
        request: Request,
        q: str = "",
        kind: str | None = None,
        source_id: str | None = None,
        zone_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        try:
            query = SearchQuery.model_validate(
                {
                    "q": q,
                    "kind": kind,
                    "source_id": source_id,
                    "zone_id": zone_id,
                    "since": since,
                    "until": until,
                    "limit": limit,
                }
            )
        except ValidationError as exc:
            raise HTTPException(422, jsonable_encoder(exc.errors(include_context=False))) from exc
        return await casework.search(query, actor(request))

    @app.get("/api/review/saved-searches", tags=["casework"])
    async def saved_searches(request: Request) -> dict[str, Any]:
        principal = actor(request)
        rows = await store.list(current_tenant(), "saved_search", limit=500)
        return {
            "saved_searches": [
                row
                for row in rows
                if row.get("owner") == principal.subject or row.get("shared") is True
            ]
        }

    @app.post("/api/review/saved-searches", tags=["casework"])
    async def save_search(body: SavedSearchCreate, request: Request) -> dict[str, Any]:
        principal = actor(request)
        search_id = new_id("search")
        return await store.put(
            current_tenant(),
            "saved_search",
            search_id,
            {**body.model_dump(mode="json"), "search_id": search_id, "owner": principal.subject},
            expected_revision=0,
        )

    @app.get("/api/review/metrics", tags=["casework"])
    async def metrics(request: Request) -> dict[str, Any]:
        return case_metrics(await casework.visible_cases(actor(request)), scan_limit=SCAN_LIMIT)


def source_safe_filename(value: str) -> str:
    return (
        "".join(char for char in value if char.isascii() and (char.isalnum() or char in "_-"))[:100]
        or "case-report"
    )
