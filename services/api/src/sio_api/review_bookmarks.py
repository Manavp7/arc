"""Private working notes at exact positions in retained recording analyses."""

from __future__ import annotations

import asyncio
import math
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import Field, field_validator, model_validator

from sio_core.tenancy import current_tenant

from .case_evidence import source_evidence_zones
from .case_helpers import StrictBody
from .cases import actor, need, review_video_available
from .video_jobs import video_mutation_lock
from .workbench_store import WorkbenchConflict

KIND = "review_bookmark"
SCAN_LIMIT = 5000
MAX_PER_VIDEO = 200
MAX_PER_AUTHOR = 1000


class BookmarkCreate(StrictBody):
    analysis_id: str = Field(min_length=1, max_length=120)
    at_s: float = Field(ge=0, allow_inf_nan=False)
    title: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=2000)

    @field_validator("title", "note", mode="before")
    @classmethod
    def trim_text(cls, value):
        return value.strip() if isinstance(value, str) else value


class BookmarkPatch(StrictBody):
    expected_revision: int = Field(ge=1, strict=True)
    title: str | None = Field(default=None, min_length=1, max_length=120)
    note: str | None = Field(default=None, max_length=2000)
    at_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator("title", "note", mode="before")
    @classmethod
    def trim_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def require_changes(self):
        changes = self.model_fields_set - {"expected_revision"}
        if not changes or any(getattr(self, key) is None for key in changes):
            raise ValueError("Provide a title, note or timestamp; bookmark fields cannot be null")
        return self


class BookmarkDelete(StrictBody):
    expected_revision: int = Field(ge=1, strict=True)


def write_lock(store):
    # Quotas span recordings. All bookmark writers acquire this before the video
    # lifecycle lock; cleanup acquires only the latter, so there is no lock cycle.
    if not hasattr(store, "_review_bookmark_write_lock"):
        store._review_bookmark_write_lock = asyncio.Lock()
    return store._review_bookmark_write_lock


class ReviewBookmarks:
    def __init__(self, store: Any):
        self.store = store

    @staticmethod
    def identity(principal, *, write=False):
        if (
            not principal.subject
            or principal.subject == "anonymous"
            or principal.tenant_id != current_tenant()
        ):
            raise HTTPException(401, "An authenticated tenant identity is required")
        need(principal, "review.read")
        need(principal, "media.read")
        if write:
            need(principal, "review.write")
        return current_tenant()

    @staticmethod
    def scopes(principal, *records, write=False):
        zones = set().union(*(source_evidence_zones({"evidence": row}) for row in records))
        scope_zones: list[str | None] = list(zones) or [None]
        for zone in scope_zones:
            need(principal, "review.read", zone)
            need(principal, "media.read", zone)
            if write:
                need(principal, "review.write", zone)

    async def video(self, tenant, video_id, principal, *, write=False):
        video = await self.store.get(tenant, "video", video_id)
        if not video:
            raise HTTPException(404, "Recording not found")
        self.scopes(principal, video, write=write)
        if not review_video_available(video):
            raise HTTPException(409, "This recording is unavailable for bookmarks")
        return video

    async def source(self, tenant, video, analysis_id, principal, *, write=False):
        analysis = await self.store.get(tenant, "analysis", analysis_id)
        if not analysis or analysis.get("video_id") != video["video_id"]:
            raise HTTPException(404, "Retained analysis not found for this recording")
        self.scopes(principal, video, analysis, write=write)
        if analysis.get("status") != "completed":
            raise HTTPException(409, "Choose a completed retained analysis for the bookmark")
        return analysis

    @staticmethod
    def position(video, at_s):
        duration = video.get("duration_s")
        if (
            not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration <= 0
            or not math.isfinite(at_s)
            or not 0 <= at_s <= duration
        ):
            raise HTTPException(422, "Bookmark time must be inside this recording")

    async def owned(self, tenant, bookmark_id, principal):
        row = await self.store.get(tenant, KIND, bookmark_id)
        if not row or row.get("author") != principal.subject:
            raise HTTPException(404, "Bookmark not found")
        return row

    async def scan(self, tenant, *, complete=False):
        rows = await self.store.list(tenant, KIND, limit=SCAN_LIMIT)
        if complete and len(rows) >= SCAN_LIMIT:
            raise HTTPException(
                409,
                "Bookmark inventory is bounded; remove older notes before adding or moving bookmarks",
            )
        return rows

    async def list(self, video_id, principal, analysis_id=None):
        tenant = self.identity(principal)
        async with video_mutation_lock(self.store, tenant, video_id):
            video = await self.video(tenant, video_id, principal)
            if analysis_id:
                await self.source(tenant, video, analysis_id, principal)
            rows = await self.scan(tenant)
            visible = []
            source_access = {}
            for row in rows:
                if row.get("author") != principal.subject or row.get("video_id") != video_id:
                    continue
                if analysis_id and row.get("analysis_id") != analysis_id:
                    continue
                source_id = row.get("analysis_id")
                if source_id not in source_access:
                    try:
                        await self.source(tenant, video, source_id, principal)
                        source_access[source_id] = True
                    except HTTPException as error:
                        if error.status_code not in {403, 404, 409}:
                            raise
                        source_access[source_id] = False
                if not source_access[source_id]:
                    continue
                visible.append(row)
            visible.sort(key=lambda row: (row["at_s"], row["analysis_id"], row["bookmark_id"]))
            return {
                "bookmarks": visible[:MAX_PER_VIDEO],
                "limit": MAX_PER_VIDEO,
                "possibly_truncated": len(rows) >= SCAN_LIMIT or len(visible) > MAX_PER_VIDEO,
            }

    async def create(self, video_id, body: BookmarkCreate, principal):
        tenant = self.identity(principal, write=True)
        async with write_lock(self.store), video_mutation_lock(self.store, tenant, video_id):
            video = await self.video(tenant, video_id, principal, write=True)
            await self.source(tenant, video, body.analysis_id, principal, write=True)
            self.position(video, body.at_s)
            rows = [
                row
                for row in await self.scan(tenant, complete=True)
                if row.get("author") == principal.subject
            ]
            same_video = [row for row in rows if row.get("video_id") == video_id]
            duplicate = next(
                (
                    row
                    for row in same_video
                    if row.get("analysis_id") == body.analysis_id and row.get("at_s") == body.at_s
                ),
                None,
            )
            if duplicate:
                return duplicate
            if len(same_video) >= MAX_PER_VIDEO or len(rows) >= MAX_PER_AUTHOR:
                raise HTTPException(
                    409, "Bookmark limit reached: remove personal notes before adding more"
                )
            bookmark_id = "bookmark_" + uuid.uuid4().hex
            return await self.store.put(
                tenant,
                KIND,
                bookmark_id,
                {
                    **body.model_dump(),
                    "bookmark_id": bookmark_id,
                    "video_id": video_id,
                    "author": principal.subject,
                },
                expected_revision=0,
            )

    async def patch(self, bookmark_id, body: BookmarkPatch, principal):
        tenant = self.identity(principal, write=True)
        async with write_lock(self.store):
            row = await self.owned(tenant, bookmark_id, principal)
            async with video_mutation_lock(self.store, tenant, row["video_id"]):
                video = await self.video(tenant, row["video_id"], principal, write=True)
                await self.source(tenant, video, row["analysis_id"], principal, write=True)
                changes = body.model_dump(exclude={"expected_revision"}, exclude_unset=True)
                self.position(video, changes.get("at_s", row["at_s"]))
                if (
                    "at_s" in changes
                    and changes["at_s"] != row["at_s"]
                    and any(
                        other["bookmark_id"] != bookmark_id
                        and other.get("author") == principal.subject
                        and other.get("video_id") == row["video_id"]
                        and other.get("analysis_id") == row["analysis_id"]
                        and other.get("at_s") == changes["at_s"]
                        for other in await self.scan(tenant, complete=True)
                    )
                ):
                    raise HTTPException(
                        409, "You already bookmarked this position in this analysis"
                    )
                try:
                    return await self.store.put(
                        tenant,
                        KIND,
                        bookmark_id,
                        {**row, **changes},
                        expected_revision=body.expected_revision,
                    )
                except WorkbenchConflict as error:
                    raise HTTPException(
                        409, "This bookmark changed. Reload before saving."
                    ) from error

    async def delete(self, bookmark_id, body: BookmarkDelete, principal):
        tenant = self.identity(principal, write=True)
        async with write_lock(self.store):
            row = await self.owned(tenant, bookmark_id, principal)
            async with video_mutation_lock(self.store, tenant, row["video_id"]):
                video = await self.video(tenant, row["video_id"], principal, write=True)
                await self.source(tenant, video, row["analysis_id"], principal, write=True)
                try:
                    await self.store.delete(
                        tenant, KIND, bookmark_id, expected_revision=body.expected_revision
                    )
                except WorkbenchConflict as error:
                    raise HTTPException(
                        409, "This bookmark changed. Reload before removing it."
                    ) from error
                return {"deleted": True, "bookmark_id": bookmark_id}


def install_review_bookmark_routes(app: FastAPI, store: Any):
    bookmarks = ReviewBookmarks(store)

    @app.get("/api/review/videos/{video_id}/bookmarks", tags=["review-bookmarks"])
    async def list_bookmarks(
        video_id: str,
        request: Request,
        analysis_id: str | None = Query(default=None, max_length=120),
    ):
        return await bookmarks.list(video_id, actor(request), analysis_id)

    @app.post("/api/review/videos/{video_id}/bookmarks", tags=["review-bookmarks"])
    async def create_bookmark(video_id: str, body: BookmarkCreate, request: Request):
        return await bookmarks.create(video_id, body, actor(request))

    @app.patch("/api/review/bookmarks/{bookmark_id}", tags=["review-bookmarks"])
    async def patch_bookmark(bookmark_id: str, body: BookmarkPatch, request: Request):
        return await bookmarks.patch(bookmark_id, body, actor(request))

    @app.delete("/api/review/bookmarks/{bookmark_id}", tags=["review-bookmarks"])
    async def delete_bookmark(bookmark_id: str, body: BookmarkDelete, request: Request):
        return await bookmarks.delete(bookmark_id, body, actor(request))

    return bookmarks
