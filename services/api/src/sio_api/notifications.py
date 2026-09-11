"""Durable personal in-app notifications, reconciled against current source access."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import Field, field_validator

from .case_evidence import source_evidence_zones
from .case_helpers import StrictBody, parse_time
from .cases import Casework, actor, need, review_video_available
from .workbench_store import WorkbenchConflict

SCAN_LIMIT = 5000
SOURCE_KINDS = ("case", "video_job", "evidence_package")
TERMINAL = {
    "video_job": {"completed", "failed"},
    "evidence_package": {"ready", "failed", "interrupted"},
}


def now_utc() -> datetime:
    return datetime.now(UTC)


def stamp(value: Any) -> str | None:
    parsed = parse_time(value)
    return parsed.astimezone(UTC).isoformat() if parsed else None


def identifier(*parts: Any) -> str:
    return (
        "notice_"
        + hashlib.sha256(
            json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:32]
    )


class NotificationSelection(StrictBody):
    notification_id: str = Field(pattern=r"^notice_[a-f0-9]{32}$")
    revision: int = Field(ge=1)


class MarkRead(StrictBody):
    notifications: list[NotificationSelection] = Field(min_length=1, max_length=100)

    @field_validator("notifications")
    @classmethod
    def distinct(cls, value):
        if len({item.notification_id for item in value}) != len(value):
            raise ValueError("Select each notification once")
        return value


def cursor_for(record: dict) -> str:
    return (
        base64.urlsafe_b64encode(
            json.dumps([record["created_at"], record["notification_id"]]).encode()
        )
        .decode()
        .rstrip("=")
    )


def read_cursor(value: str | None) -> tuple[str, str] | None:
    if value is None:
        return None
    try:
        decoded = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if (
            not isinstance(decoded, list)
            or len(decoded) != 2
            or not all(isinstance(item, str) for item in decoded)
        ):
            raise ValueError
        if not parse_time(decoded[0]) or not decoded[1].startswith("notice_"):
            raise ValueError
        return decoded[0], decoded[1]
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(422, "Invalid notification page cursor") from None


class NotificationManager:
    def __init__(
        self,
        settings: Any,
        store: Any,
        pool: Any,
        *,
        clock: Callable[[], datetime] = now_utc,
        interval_s: float = 30,
    ):
        self.settings, self.store = settings, store
        self.casework = Casework(store, pool)
        self.clock, self.interval_s = clock, interval_s
        self.boot_time = clock()
        self.lock = asyncio.Lock()
        self.recipient_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.stop = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def start(self):
        self.stop.clear()
        await self.reconcile_all()
        self.task = asyncio.create_task(self.run(), name="in-app-notifications")

    async def close(self):
        self.stop.set()
        if self.task:
            await self.task

    async def run(self):
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.interval_s)
            except TimeoutError:
                try:
                    await self.reconcile_all()
                except Exception:
                    logging.getLogger(__name__).exception(
                        "In-app notification reconciliation failed; retrying on the next interval"
                    )

    async def reconcile_all(self):
        tenants = {self.settings.tenant_id}
        if hasattr(self.store, "list_tenants"):
            for kind in SOURCE_KINDS:
                tenants.update(await self.store.list_tenants(kind))
        for tenant in sorted(tenants):
            await self.reconcile(tenant)

    async def emit(
        self,
        tenant,
        recipient,
        kind,
        source_kind,
        source,
        condition,
        *,
        title,
        message,
        target,
        severity="info",
    ):
        source_id = (
            source.get(
                {"case": "case_id", "video_job": "job_id", "evidence_package": "package_id"}[
                    source_kind
                ]
            )
            or source["record_id"]
        )
        notice_id = identifier(tenant, recipient, source_kind, source_id, kind, condition)
        try:
            await self.store.put(
                tenant,
                "notification",
                notice_id,
                {
                    "notification_id": notice_id,
                    "recipient": recipient,
                    "kind": kind,
                    "source_kind": source_kind,
                    "source_id": source_id,
                    "source_revision": source["revision"],
                    "condition": condition,
                    "title": title,
                    "message": message,
                    "target": target,
                    "severity": severity,
                    "read_at": None,
                    "occurred_at": self.clock().isoformat(),
                },
                expected_revision=0,
            )
        except WorkbenchConflict:
            # Emit-before-marker ordering can repeat after a crash; never reset personal read state.
            if await self.store.get(tenant, "notification", notice_id) is None:
                raise

    async def reconcile(self, tenant: str):
        async with self.lock:
            now = self.clock()
            baseline = await self.store.get(tenant, "notification_state", "baseline")
            if baseline is None:
                baseline = await self.store.put(
                    tenant,
                    "notification_state",
                    "baseline",
                    {"baseline_at": self.boot_time.isoformat()},
                    expected_revision=0,
                )
            complete = True
            for kind in SOURCE_KINDS:
                rows = await self.store.list(tenant, kind, limit=SCAN_LIMIT)
                complete = complete and len(rows) < SCAN_LIMIT
                for source in rows:
                    source_id = (
                        source.get(
                            {
                                "case": "case_id",
                                "video_job": "job_id",
                                "evidence_package": "package_id",
                            }[kind]
                        )
                        or source["record_id"]
                    )
                    marker_id = f"{kind}:{source_id}"
                    old = await self.store.get(tenant, "notification_source", marker_id)
                    previous = old.get("state", {}) if old else {}
                    if kind == "case":
                        state = await self.reconcile_case(tenant, source, previous, now)
                    else:
                        state = await self.reconcile_terminal(
                            tenant, kind, source, previous, parse_time(baseline["baseline_at"]), now
                        )
                    if old is None or state != previous:
                        await self.store.put(
                            tenant,
                            "notification_source",
                            marker_id,
                            {"state": state},
                            expected_revision=old["revision"] if old else 0,
                        )
            health = await self.store.get(tenant, "notification_state", "scan")
            if health is None or health.get("complete") != complete:
                await self.store.put(
                    tenant,
                    "notification_state",
                    "scan",
                    {"complete": complete},
                    expected_revision=health["revision"] if health else 0,
                )

    async def reconcile_case(self, tenant, case, previous, now):
        owner = case.get("owner") or None
        due = stamp(case.get("due_at"))
        active = case.get("status") != "resolved"
        assignment_generation = previous.get("assignment_generation", 0) + int(
            owner != previous.get("owner")
        )
        deadline_generation = previous.get("deadline_generation", 0) + int(
            due != previous.get("due_at") or (previous.get("active") is False and active)
        )
        parsed_due = parse_time(due)
        phase = (
            "overdue"
            if active and parsed_due and parsed_due < now
            else "due_soon"
            if active and parsed_due and parsed_due <= now + timedelta(hours=24)
            else None
        )
        state = {
            "owner": owner,
            "due_at": due,
            "active": active,
            "phase": phase,
            "assignment_generation": assignment_generation,
            "deadline_generation": deadline_generation,
        }
        if not owner or not active:
            return state
        condition = {"owner": owner, "assignment_generation": assignment_generation}
        target = {"kind": "case", "id": case["case_id"], "case_id": case["case_id"]}
        if owner != previous.get("owner"):
            await self.emit(
                tenant,
                owner,
                "case_assigned",
                "case",
                case,
                condition,
                title="Case assigned to you",
                message=case.get("title", "Assigned case"),
                target=target,
            )
        if phase and (
            phase != previous.get("phase")
            or owner != previous.get("owner")
            or due != previous.get("due_at")
        ):
            await self.emit(
                tenant,
                owner,
                f"case_{phase}",
                "case",
                case,
                {**condition, "due_at": due, "deadline_generation": deadline_generation},
                title="Case overdue" if phase == "overdue" else "Case due within 24 hours",
                message=case.get("title", "Assigned case"),
                target=target,
                severity="warning" if phase == "overdue" else "info",
            )
        return state

    async def reconcile_terminal(self, tenant, kind, source, previous, baseline, now):
        status = source.get("status")
        recipient = source.get("created_by")
        state = {
            "status": status,
            "created_by": recipient,
            "analysis_id": source.get("analysis_id"),
        }
        if status not in TERMINAL[kind] or not recipient or state == previous:
            return state
        event_time = parse_time(
            source.get("finished_at") or source.get("updated_at") or source.get("created_at")
        )
        if not previous and (not event_time or not baseline or event_time <= baseline):
            # First installation records old terminal work as baseline instead of flooding the bell.
            return state
        success = status in {"completed", "ready"}
        if kind == "video_job":
            target = {
                "kind": "analysis",
                "id": source["analysis_id"],
                "video_id": source["video_id"],
                "analysis_id": source["analysis_id"],
            }
            event_kind = "analysis_completed" if success else "analysis_failed"
            title = "Footage analysis completed" if success else "Footage analysis failed"
            message = source.get("video_title", "Recorded footage")
        else:
            target = {
                "kind": "package",
                "id": source["package_id"],
                "package_id": source["package_id"],
                "case_id": source["case_id"],
                "video_id": source.get("video_id"),
                "analysis_id": source.get("analysis_id"),
            }
            event_kind = "package_completed" if success else "package_failed"
            title = "Evidence export ready" if success else "Evidence export needs attention"
            message = source.get("case_title", "Evidence package")
        await self.emit(
            tenant,
            recipient,
            event_kind,
            kind,
            source,
            state,
            title=title,
            message=message,
            target=target,
            severity="info" if success else "error",
        )
        return state

    async def visible(self, tenant, notice, principal, *, now=None):
        if (
            not notice
            or notice.get("recipient") != principal.subject
            or principal.tenant_id != tenant
        ):
            return False
        now = now or self.clock()
        source = await self.store.get(tenant, notice["source_kind"], notice["source_id"])
        if source is None:
            return False
        try:
            if notice["source_kind"] == "case":
                if source.get("owner") != principal.subject or source.get("status") == "resolved":
                    return False
                await self.casework.load(source["case_id"], principal)
                marker = await self.store.get(
                    tenant, "notification_source", f"case:{source['case_id']}"
                )
                state = (marker or {}).get("state", {})
                condition = notice["condition"]
                if condition.get("assignment_generation") != state.get("assignment_generation"):
                    return False
                if notice["kind"] != "case_assigned":
                    due = parse_time(source.get("due_at"))
                    if (
                        not due
                        or stamp(due) != condition.get("due_at")
                        or condition.get("deadline_generation") != state.get("deadline_generation")
                    ):
                        return False
                    if notice["kind"] == "case_overdue" and due >= now:
                        return False
                    if notice["kind"] == "case_due_soon" and not now <= due <= now + timedelta(
                        hours=24
                    ):
                        return False
            elif notice["source_kind"] == "video_job":
                if (
                    source.get("created_by") != principal.subject
                    or source.get("analysis_id") != notice["target"]["analysis_id"]
                    or source.get("status") != notice["condition"]["status"]
                ):
                    return False
                need(principal, "review.read")
                video = await self.store.get(tenant, "video", source["video_id"])
                analysis = await self.store.get(tenant, "analysis", source["analysis_id"])
                if (
                    not review_video_available(video)
                    or not analysis
                    or analysis.get("video_id") != source["video_id"]
                ):
                    return False
                for zone in source_evidence_zones({"evidence": analysis}):
                    need(principal, "review.read", zone)
            elif notice["source_kind"] == "evidence_package":
                if (
                    source.get("created_by") != principal.subject
                    or source.get("status") != notice["condition"]["status"]
                ):
                    return False
                await self.casework.load(source["case_id"], principal)
                for action in source.get(
                    "required_actions", ["case.read", "review.read", "media.read"]
                ):
                    need(principal, action)
                for zone in source.get("case_evidence_zone_ids", []):
                    for action in ("case.read", "review.read", "media.read"):
                        need(principal, action, zone)
                for zone in source.get("included_zone_ids", []):
                    need(principal, "mission.read", zone)
            else:
                return False
        except HTTPException:
            return False
        return True

    async def list_for(self, tenant, principal, *, unread_only=False, limit=20, before=None):
        cursor = read_cursor(before)
        await self.reconcile(tenant)
        records = await self.store.list(tenant, "notification", limit=SCAN_LIMIT)
        visible = [record for record in records if await self.visible(tenant, record, principal)]
        unread_count = sum(not item.get("read_at") for item in visible)
        rows = [row for row in visible if not unread_only or not row.get("read_at")]
        rows.sort(key=lambda item: (item["created_at"], item["notification_id"]), reverse=True)
        if cursor:
            rows = [row for row in rows if (row["created_at"], row["notification_id"]) < cursor]
        selected = rows[:limit]
        state = await self.store.get(tenant, "notification_state", "scan")
        return {
            "notifications": selected,
            "unread_count": unread_count,
            "counts_complete": len(records) < SCAN_LIMIT and bool((state or {}).get("complete")),
            "has_more": len(rows) > limit,
            "next_cursor": cursor_for(selected[-1]) if len(rows) > limit else None,
            "limit": limit,
            "scan_limit": SCAN_LIMIT,
            "checked_at": self.clock().isoformat(),
            "note": "Personal in-app notifications; availability follows current ownership and access. Counts cover the latest 5000 tenant notification records and bounded source scans.",
        }

    async def target(self, tenant, principal, notification_id):
        notice = await self.store.get(tenant, "notification", notification_id)
        if not await self.visible(tenant, notice, principal):
            raise HTTPException(
                404, "This notification is no longer available under your current access"
            )
        return {"target": notice["target"], "notification_id": notification_id}

    async def mark(self, tenant, principal, body):
        lock = self.recipient_locks.setdefault((tenant, principal.subject), asyncio.Lock())
        async with lock:
            records = []
            for item in body.notifications:
                notice = await self.store.get(tenant, "notification", item.notification_id)
                if not await self.visible(tenant, notice, principal):
                    raise HTTPException(
                        404,
                        "A selected notification is no longer available under your current access",
                    )
                if notice["revision"] != item.revision:
                    raise HTTPException(
                        409, "A selected notification changed; refresh before marking this snapshot"
                    )
                records.append(notice)
            result = []
            for notice in records:
                result.append(
                    notice
                    if notice.get("read_at")
                    else await self.store.put(
                        tenant,
                        "notification",
                        notice["notification_id"],
                        {**notice, "read_at": self.clock().isoformat()},
                        expected_revision=notice["revision"],
                    )
                )
            return {"notifications": result}


def install_notification_routes(app: FastAPI, settings: Any, store: Any, pool: Any):
    from sio_core.tenancy import current_tenant

    manager = NotificationManager(settings, store, pool)

    @app.get("/api/notifications", tags=["notifications"])
    async def notifications(
        request: Request,
        unread_only: bool = False,
        limit: int = Query(default=20, ge=1, le=100),
        before: str | None = Query(default=None, max_length=512),
    ):
        return await manager.list_for(
            current_tenant(), actor(request), unread_only=unread_only, limit=limit, before=before
        )

    @app.get("/api/notifications/{notification_id}/target", tags=["notifications"])
    async def target(notification_id: str, request: Request):
        return await manager.target(current_tenant(), actor(request), notification_id)

    @app.post("/api/notifications/read", tags=["notifications"])
    async def mark(body: MarkRead, request: Request):
        principal = actor(request)
        need(principal, "notifications.write")
        return await manager.mark(current_tenant(), principal, body)

    return manager
