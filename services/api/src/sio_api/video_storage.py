"""Tenant storage inventory, reversible archive, and explicitly confirmed purge."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from sio_core.guard import principal_of
from sio_core.tenancy import current_tenant

from .video_jobs import ACTIVE_JOB_STATES, MAX_ATTEMPTS, MAX_PENDING, timestamp, video_mutation_lock


class RevisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)


class RetentionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    archive_after_days: int = Field(ge=1, le=3650)


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    video_ids: list[str] | None = Field(default=None, max_length=20)


class VideoRevision(RevisionBody):
    video_id: str


class ArchiveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    videos: list[VideoRevision] = Field(min_length=1, max_length=20)


class PurgeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_token: str
    confirmed_video_ids: list[str] = Field(min_length=1, max_length=20)


def contains_reference(value: Any, video_id: str) -> bool:
    if isinstance(value, dict):
        return any(contains_reference(item, video_id) for item in value.values())
    if isinstance(value, list):
        return any(contains_reference(item, video_id) for item in value)
    return value == video_id


def reference_values(value: Any) -> set[str]:
    """Index each bounded document once, including immutable nested attachments."""
    if isinstance(value, dict):
        return set().union(*(reference_values(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(reference_values(item) for item in value))
    return {value} if isinstance(value, str) else set()


def directory_bytes(directory: Path) -> int:
    if not directory.exists():
        return 0
    total = 0
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError("Storage contains a symbolic link; manual inspection is required")
        if path.is_file():
            total += path.stat().st_size
    return total


class VideoStorage:
    REFERENCE_KINDS = (
        "case",
        "evaluation_draft",
        "evaluation_annotations",
        "evaluation_report",
        "evidence_package",
        "case_comparison",
        "review_bookmark",
        "movement_report",
    )

    def __init__(self, manager):
        self.manager = manager
        self.store = manager.store

    async def settings(self, tenant):
        return await self.store.get(tenant, "storage_settings", "review") or {
            "revision": 0,
            "archive_after_days": 30,
            "automatic_purge": False,
            "automatic_archive": False,
        }

    async def reference_index(self, tenant):
        references = {}
        complete = True
        for kind in self.REFERENCE_KINDS:
            rows = await self.store.list(tenant, kind, limit=5000)
            complete = complete and len(rows) < 5000
            references[kind] = rows
        jobs = await self.manager.jobs(tenant)
        return references, jobs, complete and len(jobs) < 5000

    async def candidates(self, tenant, video_ids=None, *, purge=False, manual=False):
        records = await self.store.list(tenant, "video", limit=5000)
        inventory_complete = len(records) < 5000
        if video_ids is not None:
            wanted = set(video_ids)
            if len(wanted) != len(video_ids):
                raise HTTPException(422, "Select each video only once")
            for video_id in wanted:
                self.manager.directory(tenant, video_id)
            records = [row for row in records if row["video_id"] in wanted]
            if {row["video_id"] for row in records} != wanted:
                raise HTTPException(404, "A selected video is unavailable")
        references, jobs, complete = await self.reference_index(tenant)
        analyses = await self.store.list(tenant, "analysis", limit=5000)
        complete = complete and len(analyses) < 5000
        analyses_by_video: dict[str, list[str]] = {}
        for analysis in analyses:
            if analysis.get("video_id") and analysis.get("analysis_id"):
                analyses_by_video.setdefault(analysis["video_id"], []).append(
                    analysis["analysis_id"]
                )
        indexed_references = {
            kind: [reference_values(row) for row in rows] for kind, rows in references.items()
        }
        settings = await self.settings(tenant)
        cutoff = datetime.now(UTC) - timedelta(days=settings["archive_after_days"])
        results = []
        for video in records:
            video_id = video["video_id"]
            protected_ids = {video_id, *analyses_by_video.get(video_id, [])}
            reasons = []
            if not inventory_complete:
                reasons.append("storage_inventory_incomplete")
            linked = {
                kind: sum(bool(protected_ids & row) for row in rows)
                for kind, rows in indexed_references.items()
            }
            if not complete:
                reasons.append("reference_scan_incomplete")
            if linked["case"]:
                reasons.append("case_linked")
            if any(
                linked[kind]
                for kind in ("evaluation_draft", "evaluation_annotations", "evaluation_report")
            ):
                reasons.append("evaluation_linked")
            if linked["review_bookmark"]:
                reasons.append("bookmark_linked")
            if linked["movement_report"]:
                reasons.append("movement_report_linked")
            if linked["evidence_package"]:
                reasons.append("evidence_package_linked")
            if any(
                job["video_id"] == video_id and job["status"] in ACTIVE_JOB_STATES for job in jobs
            ):
                reasons.append("active_job")
            if video.get("status") in {"analyzing", "queued", "running"}:
                reasons.append("active_video")
            if video.get("purged_at"):
                reasons.append("already_purged")
            if purge and not video.get("archived_at"):
                reasons.append("archive_first")
            if not purge and video.get("archived_at"):
                reasons.append("already_archived")
            if not purge and not manual:
                try:
                    created = datetime.fromisoformat(video["created_at"].replace("Z", "+00:00"))
                    if created.tzinfo is None or created > cutoff:
                        reasons.append("within_retention_period")
                except (ValueError, KeyError):
                    reasons.append("unknown_creation_date")
            try:
                size = await asyncio.to_thread(
                    directory_bytes, self.manager.directory(tenant, video_id)
                )
            except (OSError, ValueError, HTTPException):
                size = None
                reasons.append("storage_inspection_failed")
            results.append(
                {
                    **{
                        key: video.get(key)
                        for key in (
                            "video_id",
                            "title",
                            "revision",
                            "created_at",
                            "status",
                            "archived_at",
                            "purged_at",
                            "purge_error",
                        )
                    },
                    "bytes": size,
                    "eligible": not reasons,
                    "reasons": reasons,
                    "linked_cases": linked["case"],
                    "linked_evaluations": sum(
                        linked[kind]
                        for kind in (
                            "evaluation_draft",
                            "evaluation_annotations",
                            "evaluation_report",
                        )
                    ),
                    "linked_packages": linked["evidence_package"],
                    "linked_bookmarks": linked["review_bookmark"],
                    "linked_movement_reports": linked["movement_report"],
                }
            )
        return results

    async def archive(self, tenant, selected, actor):
        ids = [item.video_id for item in selected]
        if len(set(ids)) != len(ids):
            raise HTTPException(422, "Select each video only once")
        async with contextlib.AsyncExitStack() as stack:
            for video_id in sorted(ids):
                await stack.enter_async_context(video_mutation_lock(self.store, tenant, video_id))
            candidates = await self.candidates(tenant, ids, manual=True)
            revisions = {item.video_id: item.revision for item in selected}
            if any(
                not item["eligible"] or item["revision"] != revisions[item["video_id"]]
                for item in candidates
            ):
                raise HTTPException(
                    409, "A selected video changed or is protected; refresh the cleanup preview"
                )
            archived = []
            for item in candidates:
                video = await self.manager.video(tenant, item["video_id"])
                archived.append(
                    await self.store.put(
                        tenant,
                        "video",
                        item["video_id"],
                        {**video, "archived_at": timestamp(), "archived_by": actor},
                        expected_revision=item["revision"],
                    )
                )
            return {
                "videos": archived,
                "reclaimed_bytes": 0,
                "note": "Archived records are hidden from the active library. Media remains private and can be restored; archive does not free disk space.",
            }

    async def purge_preview(self, tenant, video_ids, actor):
        if not video_ids:
            raise HTTPException(422, "Select archived videos to preview permanent deletion")
        videos = await self.candidates(tenant, video_ids, purge=True, manual=True)
        preview_token = "purge_" + uuid.uuid4().hex
        expires_at = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        await self.store.put(
            tenant,
            "storage_preview",
            preview_token,
            {
                "preview_token": preview_token,
                "created_by": actor,
                "expires_at": expires_at,
                "videos": [
                    {"video_id": row["video_id"], "revision": row["revision"]}
                    for row in videos
                    if row["eligible"]
                ],
                "used": False,
            },
            expected_revision=0,
        )
        return {
            "preview_token": preview_token,
            "expires_at": expires_at,
            "videos": videos,
            "reclaimable_bytes": sum(row["bytes"] or 0 for row in videos if row["eligible"]),
        }

    async def purge(self, tenant, body, actor):
        preview = await self.store.get(tenant, "storage_preview", body.preview_token)
        if not preview or preview["created_by"] != actor:
            raise HTTPException(404, "Purge preview not found")
        if preview["used"] or datetime.fromisoformat(preview["expires_at"]) <= datetime.now(UTC):
            raise HTTPException(409, "Purge preview expired or was used; preview again")
        selected = {item["video_id"]: item["revision"] for item in preview["videos"]}
        ids = body.confirmed_video_ids
        if len(set(ids)) != len(ids) or set(ids) != set(selected):
            raise HTTPException(422, "Confirm exactly the eligible video IDs in the purge preview")
        async with contextlib.AsyncExitStack() as stack:
            for video_id in sorted(ids):
                await stack.enter_async_context(video_mutation_lock(self.store, tenant, video_id))
            rows = await self.candidates(tenant, ids, purge=True, manual=True)
            if any(
                not row["eligible"] or row["revision"] != selected[row["video_id"]] for row in rows
            ):
                raise HTTPException(
                    409, "A video changed or acquired evidence references; preview again"
                )
            # Claim once before any irreversible action, even for concurrent identical requests.
            await self.store.put(
                tenant,
                "storage_preview",
                body.preview_token,
                {**preview, "used": True, "used_at": timestamp()},
                expected_revision=preview["revision"],
            )
            results = []
            for row in rows:
                video_id = row["video_id"]
                video = await self.manager.video(tenant, video_id)
                pending = await self.store.put(
                    tenant,
                    "video",
                    video_id,
                    {
                        **video,
                        "status": "purging",
                        "purge_started_at": timestamp(),
                        "purged_by": actor,
                    },
                    expected_revision=row["revision"],
                )
                directory = self.manager.directory(tenant, video_id)
                try:
                    # The directory was inspected for symlinks in the fresh eligibility scan.
                    if directory.exists():
                        removal = asyncio.create_task(asyncio.to_thread(shutil.rmtree, directory))
                        try:
                            await asyncio.shield(removal)
                        except asyncio.CancelledError:
                            await removal
                            raise
                except OSError:
                    await self.store.put(
                        tenant,
                        "video",
                        video_id,
                        {
                            **pending,
                            "status": "purge_failed",
                            "purge_error": "Media deletion did not finish. Remaining files stay private; inspect storage and preview again.",
                        },
                        expected_revision=pending["revision"],
                    )
                    results.append(
                        {"video_id": video_id, "status": "purge_failed", "reclaimed_bytes": 0}
                    )
                    continue
                await self.store.put(
                    tenant,
                    "video",
                    video_id,
                    {
                        **pending,
                        "status": "purged",
                        "purged_at": timestamp(),
                        "purge_error": None,
                        "media_url": None,
                        "poster_url": None,
                    },
                    expected_revision=pending["revision"],
                )
                results.append(
                    {"video_id": video_id, "status": "purged", "reclaimed_bytes": row["bytes"] or 0}
                )
            return {
                "videos": results,
                "reclaimed_bytes": sum(row["reclaimed_bytes"] for row in results),
                "note": "Database records and revision history are retained. Purged media cannot be restored.",
            }


def install_storage_routes(app: FastAPI, manager):
    storage = VideoStorage(manager)
    manager.storage = storage
    store = manager.store

    @app.get("/api/review/jobs", tags=["video-review"])
    async def jobs():
        tenant = current_tenant()
        await manager.ensure_recovered(tenant)
        records = sorted(
            await manager.jobs(tenant),
            key=lambda job: (job["queued_at"], job["job_id"]),
            reverse=True,
        )
        return {
            "jobs": records,
            "capabilities": {
                "max_attempts": MAX_ATTEMPTS,
                "max_pending": MAX_PENDING,
                "worker_concurrency": 1,
            },
        }

    @app.post("/api/review/jobs/{job_id}/cancel", tags=["video-review"])
    async def cancel(job_id: str, body: RevisionBody):
        return await manager.cancel(current_tenant(), job_id, body.revision)

    @app.post("/api/review/jobs/{job_id}/retry", status_code=202, tags=["video-review"])
    async def retry(job_id: str, body: RevisionBody, request: Request):
        return await manager.retry_job(
            current_tenant(), job_id, body.revision, principal_of(request).subject
        )

    @app.get("/api/review/storage", tags=["video-storage"])
    async def overview():
        tenant = current_tenant()
        videos = await storage.candidates(tenant)
        return {
            "videos": videos,
            "settings": await storage.settings(tenant),
            "usage": {
                "total_bytes": sum(item["bytes"] or 0 for item in videos),
                "active_bytes": sum(
                    item["bytes"] or 0 for item in videos if not item["archived_at"]
                ),
                "archived_bytes": sum(item["bytes"] or 0 for item in videos if item["archived_at"]),
                "video_count": sum(not bool(item["purged_at"]) for item in videos),
                "archived_count": sum(
                    bool(item["archived_at"]) and not bool(item["purged_at"]) for item in videos
                ),
                "inspection_complete": all(
                    item["bytes"] is not None
                    and "storage_inventory_incomplete" not in item["reasons"]
                    for item in videos
                ),
                "note": "Recorded-video files only; database, site floorplans and exported packages are not included. Archive frees no disk space.",
            },
        }

    @app.put("/api/review/storage/settings", tags=["video-storage"])
    async def settings(body: RetentionBody):
        return await store.put(
            current_tenant(),
            "storage_settings",
            "review",
            {
                "archive_after_days": body.archive_after_days,
                "automatic_purge": False,
                "automatic_archive": False,
            },
            expected_revision=body.revision,
        )

    @app.post("/api/review/storage/preview", tags=["video-storage"])
    async def preview(body: Selection):
        rows = await storage.candidates(
            current_tenant(), body.video_ids, manual=body.video_ids is not None
        )
        return {
            "videos": rows,
            "reclaimed_bytes": 0,
            "note": "Archive is reversible and does not free disk space.",
        }

    @app.post("/api/review/storage/archive", tags=["video-storage"])
    async def archive(body: ArchiveBody, request: Request):
        return await storage.archive(current_tenant(), body.videos, principal_of(request).subject)

    @app.post("/api/review/storage/restore/{video_id}", tags=["video-storage"])
    async def restore(video_id: str, body: RevisionBody):
        tenant = current_tenant()
        async with video_mutation_lock(store, tenant, video_id):
            video = await manager.video(tenant, video_id)
            if not video.get("archived_at"):
                raise HTTPException(409, "Video is not archived")
            if video.get("status") in {"purging", "purge_failed"}:
                raise HTTPException(
                    409, "Media deletion was started; this video cannot be restored"
                )
            return await store.put(
                tenant,
                "video",
                video_id,
                {**video, "archived_at": None, "archived_by": None},
                expected_revision=body.revision,
            )

    @app.post("/api/review/storage/purge-preview", tags=["video-storage"])
    async def purge_preview(body: Selection, request: Request):
        return await storage.purge_preview(
            current_tenant(), body.video_ids, principal_of(request).subject
        )

    @app.post("/api/review/storage/purge", tags=["video-storage"])
    async def purge(body: PurgeBody, request: Request):
        return await storage.purge(current_tenant(), body, principal_of(request).subject)
