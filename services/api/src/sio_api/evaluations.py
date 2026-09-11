"""Human-reviewed annotation snapshots and immutable event comparison reports."""

from __future__ import annotations

from contextlib import AsyncExitStack
from copy import deepcopy
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request

from sio_core.tenancy import current_tenant

from .case_helpers import iso_now
from .cases import actor, allowed, need
from .evaluation_metrics import (
    AnnotationDraft,
    EvaluationRequest,
    FreezeRequest,
    evaluate,
    fingerprint,
    measures,
)
from .video_jobs import video_mutation_lock

LIMIT = 5000
LIMITATIONS = [
    "Metrics apply only to the selected clips, label scopes and human-certified time intervals. Unreviewed footage remains unknown.",
    "Entry and dwell labels represent incident windows; matching uses zone plus event type, not identity or object-class accuracy. Delay is signed time from annotated window start.",
    "Authored fixtures verify the workflow and cannot establish real-site detector accuracy. Footage origin and complete annotation coverage are human declarations.",
    "A held-out split is fixed per uploaded clip. Repeated inspection can contaminate a holdout; this lab does not enforce reviewer blinding or detect re-uploads of the same footage.",
]


def can_read(principal, record: dict) -> bool:
    return allowed(principal, "review.read") and all(
        allowed(principal, "review.read", scope["zone_id"]) for scope in record.get("scopes", [])
    )


class EvaluationLab:
    def __init__(self, store: Any):
        self.store = store

    async def video(self, video_id: str, principal, *, writable=False):
        video = await self.store.get(current_tenant(), "video", video_id)
        if not video:
            raise HTTPException(404, "Video not found")
        need(principal, "review.write" if writable else "review.read")
        for zone in video.get("zones", []):
            need(principal, "review.read", zone["zone_id"])
        if writable and (video.get("archived_at") or video.get("purged_at")):
            raise HTTPException(409, "Restore the recording before creating evaluation references")
        return video

    async def versions(self, video_id: str, principal):
        video = await self.video(video_id, principal)
        analyses = [
            row
            for row in await self.store.list(current_tenant(), "analysis", limit=LIMIT)
            if row.get("video_id") == video_id
            and row.get("status") == "completed"
            and all(
                allowed(principal, "review.read", zone["zone_id"]) for zone in row.get("zones", [])
            )
        ]
        draft = await self.store.get(current_tenant(), "evaluation_draft", video_id)
        if draft and not can_read(principal, draft):
            raise HTTPException(403, "Your zone access does not permit these annotations")
        return {
            "video": video,
            "draft": draft,
            "annotation_sets": [
                row
                for row in await self.store.list(
                    current_tenant(), "evaluation_annotations", limit=LIMIT
                )
                if row.get("video_id") == video_id and can_read(principal, row)
            ],
            "analyses": [
                {
                    key: row.get(key)
                    for key in (
                        "analysis_id",
                        "video_id",
                        "status",
                        "created_at",
                        "model",
                        "rules",
                        "zones",
                        "events",
                    )
                }
                for row in analyses
            ],
            "note": "Retained versions are bounded to the latest 5000 analysis records in this tenant.",
        }

    async def save(self, video_id: str, body: AnnotationDraft, principal):
        tenant = current_tenant()
        async with video_mutation_lock(self.store, tenant, video_id):
            video = await self.video(video_id, principal, writable=True)
            if any(
                item.end_s > video["duration_s"] for item in [*body.coverage, *body.annotations]
            ):
                raise HTTPException(422, "Intervals cannot extend beyond the clip duration")
            known_zones = {zone["zone_id"] for zone in video.get("zones", [])}
            for analysis in await self.store.list(tenant, "analysis", limit=LIMIT):
                if analysis.get("video_id") == video_id and analysis.get("status") == "completed":
                    known_zones.update(zone["zone_id"] for zone in analysis.get("zones", []))
            for scope in body.scopes:
                if scope.zone_id not in known_zones:
                    raise HTTPException(
                        422, "Choose a zone from this recording or a retained analysis"
                    )
                need(principal, "review.write", scope.zone_id)
            existing = await self.store.get(tenant, "evaluation_draft", video_id)
            if (existing or {}).get("revision", 0) != body.revision:
                raise HTTPException(409, "Annotations changed. Reload before saving your work.")
            partition = await self.store.get(tenant, "evaluation_partition", video_id)
            if partition and partition["partition"] != body.partition:
                raise HTTPException(
                    409, "This clip's dataset partition is fixed to prevent split leakage"
                )
            if not partition:
                await self.store.put(
                    tenant,
                    "evaluation_partition",
                    video_id,
                    {
                        "video_id": video_id,
                        "partition": body.partition,
                        "assigned_by": principal.subject,
                    },
                    expected_revision=0,
                )
            return await self.store.put(
                tenant,
                "evaluation_draft",
                video_id,
                {
                    **body.model_dump(mode="json", exclude={"revision"}),
                    "video_id": video_id,
                    "duration_s": video["duration_s"],
                    "video_title": video["title"],
                    "updated_by": principal.subject,
                },
                expected_revision=body.revision,
            )

    async def freeze(self, video_id: str, revision: int, principal):
        tenant = current_tenant()
        async with video_mutation_lock(self.store, tenant, video_id):
            await self.video(video_id, principal, writable=True)
            draft = await self.store.get(tenant, "evaluation_draft", video_id)
            if not draft:
                raise HTTPException(404, "Save reviewed annotations first")
            if draft["revision"] != revision:
                raise HTTPException(409, "Annotations changed. Reload before freezing a version.")
            for scope in draft["scopes"]:
                need(principal, "review.write", scope["zone_id"])
            content = {
                key: value
                for key, value in draft.items()
                if key not in {"record_id", "revision", "created_at", "updated_at"}
            }
            annotation_hash = fingerprint(content)
            # Freezing unchanged work is idempotent; reports retain this exact record.
            for existing in await self.store.list(tenant, "evaluation_annotations", limit=LIMIT):
                if (
                    existing.get("video_id") == video_id
                    and existing.get("annotation_hash") == annotation_hash
                ):
                    return existing
            annotation_id = "ann_" + uuid4().hex
            return await self.store.put(
                tenant,
                "evaluation_annotations",
                annotation_id,
                {
                    **content,
                    "annotation_set_id": annotation_id,
                    "annotation_hash": annotation_hash,
                    "draft_revision": revision,
                    "frozen_by": principal.subject,
                    "frozen_at": iso_now(),
                },
                expected_revision=0,
            )

    async def report(self, body: EvaluationRequest, principal):
        tenant = current_tenant()
        need(principal, "review.write")
        sets = []
        for item in body.inputs:
            row = await self.store.get(tenant, "evaluation_annotations", item.annotation_set_id)
            if row is None:
                raise HTTPException(404, "Frozen annotations not found")
            for scope in row["scopes"]:
                need(principal, "review.read", scope["zone_id"])
            sets.append(row)
        if len({row["video_id"] for row in sets}) != len(sets):
            raise HTTPException(422, "Each clip can appear only once in a report")
        if len({row["partition"] for row in sets}) != 1:
            raise HTTPException(422, "Development, validation and held-out clips cannot be pooled")
        async with AsyncExitStack() as stack:
            for video_id in sorted(row["video_id"] for row in sets):
                await stack.enter_async_context(video_mutation_lock(self.store, tenant, video_id))
                await self.video(video_id, principal, writable=True)
            candidates: list[dict] = [
                {"candidate": index + 1, "clips": []}
                for index in range(len(body.inputs[0].analysis_ids))
            ]
            refs = []
            for item, annotations in zip(body.inputs, sets, strict=True):
                refs.append(
                    {
                        "video_id": annotations["video_id"],
                        "annotation_set_id": item.annotation_set_id,
                        "analysis_ids": item.analysis_ids,
                    }
                )
                for index, analysis_id in enumerate(item.analysis_ids):
                    analysis = await self.store.get(tenant, "analysis", analysis_id)
                    if not analysis or analysis.get("video_id") != annotations["video_id"]:
                        raise HTTPException(
                            404, "Analysis is not a retained version of this recording"
                        )
                    if analysis.get("status") != "completed":
                        raise HTTPException(409, "Evaluation requires completed analysis versions")
                    for zone in analysis.get("zones", []):
                        need(principal, "review.read", zone["zone_id"])
                    candidates[index]["clips"].append(
                        evaluate(annotations, analysis, body.tolerance_s)
                    )
            for candidate in candidates:
                clips = candidate["clips"]
                candidate["metrics"] = measures(
                    sum(clip["metrics"]["true_positive"] for clip in clips),
                    sum(clip["metrics"]["false_positive"] for clip in clips),
                    sum(clip["metrics"]["false_negative"] for clip in clips),
                    sum(clip["metrics"]["reviewed_seconds"] for clip in clips),
                    [match["signed_delay_s"] for clip in clips for match in clip["matches"]],
                )
            report_id = "eval_" + uuid4().hex
            return await self.store.put(
                tenant,
                "evaluation_report",
                report_id,
                {
                    "report_id": report_id,
                    "title": body.title,
                    "partition": sets[0]["partition"],
                    "footage_origins": sorted({row["footage_origin"] for row in sets}),
                    "inputs": refs,
                    "video_ids": [row["video_id"] for row in sets],
                    "analysis_ids": sorted(
                        {version for item in body.inputs for version in item.analysis_ids}
                    ),
                    "scopes": [scope for row in sets for scope in row["scopes"]],
                    "annotation_snapshots": deepcopy(sets),
                    "candidates": candidates,
                    "tolerance_s": body.tolerance_s,
                    "matching_version": "scope-coverage-bipartite-v1",
                    "created_by": principal.subject,
                    "limitations": LIMITATIONS,
                },
                expected_revision=0,
            )


def install_evaluation_routes(app: FastAPI, store: Any) -> EvaluationLab:
    lab = EvaluationLab(store)

    @app.get("/api/review/evaluations")
    async def overview(request: Request):
        principal = actor(request)
        need(principal, "review.read")
        reports = [
            row
            for row in await store.list(current_tenant(), "evaluation_report", limit=LIMIT)
            if can_read(principal, row)
        ]
        return {
            "reports": [
                {
                    key: value
                    for key, value in row.items()
                    if key not in {"annotation_snapshots", "candidates"}
                }
                for row in reports
            ],
            "limitations": LIMITATIONS,
        }

    @app.get("/api/review/evaluations/videos/{video_id}")
    async def versions(video_id: str, request: Request):
        return await lab.versions(video_id, actor(request))

    @app.put("/api/review/evaluations/videos/{video_id}/annotations")
    async def save(video_id: str, body: AnnotationDraft, request: Request):
        return await lab.save(video_id, body, actor(request))

    @app.post("/api/review/evaluations/videos/{video_id}/freeze")
    async def freeze(video_id: str, body: FreezeRequest, request: Request):
        return await lab.freeze(video_id, body.revision, actor(request))

    @app.post("/api/review/evaluations/reports", status_code=201)
    async def create_report(body: EvaluationRequest, request: Request):
        return await lab.report(body, actor(request))

    @app.get("/api/review/evaluations/reports/{report_id}")
    async def get_report(report_id: str, request: Request):
        principal = actor(request)
        report = await store.get(current_tenant(), "evaluation_report", report_id)
        if not report:
            raise HTTPException(404, "Evaluation report not found")
        if not can_read(principal, report):
            raise HTTPException(403, "Your role or zone access does not permit this report")
        return report

    return lab
