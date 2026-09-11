"""Reviewer-declared alignment of two immutable case evidence sources."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import Field, model_validator

from sio_core.tenancy import current_tenant

from .case_evidence import case_evidence_items, source_evidence_zones
from .case_helpers import StrictBody
from .cases import Casework, actor, need, review_video_available
from .workbench_store import WorkbenchConflict


class ComparisonSave(StrictBody):
    revision: int = Field(ge=0)
    case_revision: int = Field(ge=1)
    left_attachment_id: str = Field(min_length=1, max_length=120)
    right_attachment_id: str = Field(min_length=1, max_length=120)
    offset_s: float = Field(default=0, ge=-180, le=180, allow_inf_nan=False)
    note: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def distinct_sources(self):
        if self.left_attachment_id == self.right_attachment_id:
            raise ValueError("Choose two distinct case evidence sources")
        return self


class EvidenceComparison:
    def __init__(self, store: Any, pool: Any):
        self.store = store
        self.casework = Casework(store, pool)

    async def context(self, case_id, principal):
        record = await self.casework.load(case_id, principal)
        need(principal, "review.read")
        items = [
            item
            for item in case_evidence_items(record)
            if item.get("video_id") and item.get("analysis_id")
        ]
        return record, items

    @staticmethod
    def response(record, items, saved=None):
        saved = saved or {}
        return {
            "case_id": record["case_id"],
            "case_title": record["title"],
            "case_revision": record["revision"],
            "revision": saved.get("revision", 0),
            "saved_case_revision": saved.get("case_revision"),
            "left_attachment_id": saved.get("left_attachment_id"),
            "right_attachment_id": saved.get("right_attachment_id"),
            "offset_s": saved.get("offset_s", 0),
            "note": saved.get("note", ""),
            "updated_by": saved.get("updated_by"),
            "updated_at": saved.get("updated_at"),
            "evidence_items": items,
            "time_basis": "reviewer_declared_offset",
            "alignment_note": "Right recording time = left recording time + offset. This is a reviewer-declared alignment, not a verified shared camera clock.",
        }

    async def get(self, case_id, principal):
        record, items = await self.context(case_id, principal)
        saved = await self.store.get(current_tenant(), "case_comparison", case_id)
        return self.response(record, items, saved)

    async def validate_source(self, item, principal):
        tenant = current_tenant()
        video = await self.store.get(tenant, "video", item["video_id"])
        analysis = await self.store.get(tenant, "analysis", item["analysis_id"])
        if not review_video_available(video):
            raise HTTPException(409, "A selected recording is unavailable for comparison")
        if (
            not analysis
            or analysis.get("video_id") != item["video_id"]
            or analysis.get("status") != "completed"
        ):
            raise HTTPException(409, "A selected retained analysis is unavailable")
        zones = source_evidence_zones({"evidence": analysis}) | source_evidence_zones(item)
        scope_zones: list[str | None] = list(zones) or [None]
        for zone in scope_zones:
            need(principal, "review.read", zone)
            need(principal, "media.read", zone)

    async def save(self, case_id, body: ComparisonSave, principal):
        need(principal, "case.write")
        record, items = await self.context(case_id, principal)
        need(principal, "case.write", record.get("zone_id"))
        if record["revision"] != body.case_revision:
            raise HTTPException(409, "The case changed. Reload before saving this alignment.")
        selected = []
        for attachment_id in (body.left_attachment_id, body.right_attachment_id):
            item = next((row for row in items if row["attachment_id"] == attachment_id), None)
            if item is None:
                raise HTTPException(422, "Choose recording evidence from this case")
            await self.validate_source(item, principal)
            selected.append(item)
        # Revalidate source visibility and revision after the retained media lookups.
        current, _ = await self.context(case_id, principal)
        if current["revision"] != body.case_revision:
            raise HTTPException(409, "The case changed. Reload before saving this alignment.")
        data = {
            **body.model_dump(exclude={"revision"}),
            "case_id": case_id,
            "updated_by": principal.subject,
            "time_basis": "reviewer_declared_offset",
            "source_refs": [
                {
                    key: deepcopy(item.get(key))
                    for key in (
                        "attachment_id",
                        "video_id",
                        "analysis_id",
                        "event_id",
                        "annotation_set_id",
                        "annotation_id",
                    )
                }
                for item in selected
            ],
        }
        try:
            saved = await self.store.put(
                current_tenant(),
                "case_comparison",
                case_id,
                data,
                expected_revision=body.revision,
            )
        except WorkbenchConflict as error:
            raise HTTPException(409, "The alignment changed. Reload before saving.") from error
        return self.response(record, items, saved)


def install_evidence_comparison_routes(app: FastAPI, store: Any, pool: Any):
    comparison = EvidenceComparison(store, pool)

    @app.get("/api/cases/{case_id}/comparison", tags=["casework"])
    async def get_comparison(case_id: str, request: Request):
        return await comparison.get(case_id, actor(request))

    @app.put("/api/cases/{case_id}/comparison", tags=["casework"])
    async def save_comparison(case_id: str, body: ComparisonSave, request: Request):
        return await comparison.save(case_id, body, actor(request))

    return comparison
