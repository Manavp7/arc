"""Opt-in, bounded semantic retrieval over retained local recordings."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import Field, field_validator, model_validator

from .case_helpers import StrictBody
from .cases import actor
from .recorded_insights import RecordedInsights
from .recorded_search_model import MODEL_ID, FrameReader, check_model, file_hash, load_model, vector
from .video_jobs import timestamp, video_mutation_lock, video_processor_lock
from .video_review import blocking_media
from .workbench_store import WorkbenchConflict

KIND = "recorded_search_index"
INDEX_NAME = "recorded-search.json"
MAX_SAMPLES = 90
MAX_INDEX_BYTES = 2 * 1024 * 1024
SAMPLE_INTERVAL = 2.0
NOTE = (
    "Ranked visual similarities are search leads, not detections, identities or probabilities. "
    "At most one existing analysis sample every two seconds is indexed. Unsampled footage and "
    "low-ranked results do not establish absence. Thumbnails remain pixelated."
)


class IndexRequest(StrictBody):
    video_id: str = Field(pattern=r"^vid_[a-f0-9]{32}$")
    analysis_id: str = Field(pattern=r"^ana_[a-f0-9]{32}$")
    consent_private_original: Literal[True]

    @field_validator("consent_private_original", mode="before")
    @classmethod
    def explicit_consent(cls, value):
        if value is not True:
            raise ValueError("Explicit consent is required to index the private original")
        return value


class SampleReference(StrictBody):
    video_id: str = Field(pattern=r"^vid_[a-f0-9]{32}$")
    analysis_id: str = Field(pattern=r"^ana_[a-f0-9]{32}$")
    frame_index: int = Field(ge=0, lt=360, strict=True)


class SearchRequest(StrictBody):
    text: str = Field(default="", max_length=300)
    sample: SampleReference | None = None
    video_id: str = Field(default="", max_length=120)
    limit: int = Field(default=24, ge=1, le=50, strict=True)

    @model_validator(mode="after")
    def one_query(self):
        if bool(self.text) == bool(self.sample):
            raise ValueError("Provide a description or one indexed sample, but not both")
        return self


def source_key(video, analysis):
    # Scope/configuration/source revisions matter; a later run never silently substitutes
    # its analysis for the exact version that the operator chose to index.
    value = [video["video_id"], video["revision"], analysis["analysis_id"], analysis["revision"]]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def samples_for(video, analysis):
    duration = video.get("duration_s", 0)
    if (
        not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or not 0 < duration <= 180
    ):
        raise ValueError("Recording duration is outside the supported bounds")
    samples = []
    previous = -SAMPLE_INTERVAL
    for frame in analysis.get("detections", []):
        at, index = frame.get("at_s"), frame.get("frame_index")
        if (
            not isinstance(at, (float, int))
            or not math.isfinite(at)
            or not 0 <= at < duration
            or type(index) is not int
            or not 0 <= index < 360
        ):
            raise ValueError("Analysis sample positions are invalid")
        if at >= previous + SAMPLE_INTERVAL:
            samples.append({"at_s": at, "frame_index": index})
            previous = at
    if not samples or len(samples) > MAX_SAMPLES:
        raise ValueError("A completed analysis with at most 90 selected samples is required")
    return samples


def private_file(directory, name, *, exists=True):
    path = directory / name
    if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
        raise ValueError("Private search media is unavailable")
    if exists and not path.is_file():
        raise ValueError("Private search media is unavailable")
    return path


def write_index(directory, payload):
    target = private_file(directory, INDEX_NAME, exists=False)
    encoded = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_INDEX_BYTES:
        raise ValueError("Search index exceeds its size limit")
    fd, temporary = tempfile.mkstemp(prefix=".recorded-search-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).replace(target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_index(directory):
    path = private_file(directory, INDEX_NAME)
    if path.stat().st_size > MAX_INDEX_BYTES:
        raise ValueError("Search index exceeds its size limit")
    with path.open("rb") as handle:
        payload = json.loads(handle.read(MAX_INDEX_BYTES + 1))
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("samples"), list)
        or not 1 <= len(payload["samples"]) <= MAX_SAMPLES
    ):
        raise ValueError("Search index is incomplete")
    for sample in payload["samples"]:
        vector(sample["vector"])
    return payload


class RecordedSearch(RecordedInsights):
    def __init__(self, settings, store, video_manager):
        super().__init__(store)
        self.settings = settings
        self.video_manager = video_manager
        self.enqueue_lock = asyncio.Lock()
        self.model_lock = asyncio.Lock()
        self.task = None
        self.closing = False
        self.model = None

    async def start(self):
        self.closing = False
        tenants = {self.settings.tenant_id}
        if hasattr(self.store, "list_tenants"):
            tenants.update(await self.store.list_tenants(KIND))
        for tenant in tenants:
            for row in await self.store.list(tenant, KIND, limit=5000):
                if row.get("status") in {"queued", "running"}:
                    await self.store.put(
                        tenant,
                        KIND,
                        row["video_id"],
                        {
                            **row,
                            "status": "interrupted",
                            "error": "Indexing stopped. Review consent and retry.",
                        },
                        expected_revision=row["revision"],
                    )

    async def close(self):
        self.closing = True
        if self.task:
            await self.task
        async with self.model_lock:
            if self.model is not None:
                await blocking_media(self.model.close)
                self.model = None

    async def runtime(self):
        available = await blocking_media(check_model, self.settings)
        if not available["available"]:
            raise HTTPException(503, available["reason"])
        if self.model is None:
            try:
                self.model = await blocking_media(load_model, self.settings)
            except Exception as error:
                raise HTTPException(
                    503, "Pinned CLIP assets could not load; inspect the local installation"
                ) from error
        return self.model

    def directory(self, tenant, video_id):
        return self.video_manager.directory(tenant, video_id)

    @staticmethod
    def public_status(row):
        return {
            key: row.get(key)
            for key in (
                "video_id",
                "analysis_id",
                "status",
                "progress",
                "sample_count",
                "model_id",
                "error",
                "requested_at",
                "finished_at",
            )
        }

    async def catalog(self, principal):
        tenant = self.identity(principal)
        available, truncated = await self.candidates(principal)
        videos = []
        for video, versions in available:
            index = await self.store.get(tenant, KIND, video["video_id"])
            if index:
                try:
                    await self.source(tenant, video, index["analysis_id"], principal)
                except HTTPException as error:
                    if error.status_code not in {403, 404, 409}:
                        raise
                    index = None
            if index:
                run = next(
                    (run for run in versions if run["analysis_id"] == index["analysis_id"]), None
                )
                if index["status"] == "ready" and (
                    run is None
                    or source_key(video, run) != index.get("source_key")
                    or not (self.directory(tenant, video["video_id"]) / INDEX_NAME).is_file()
                ):
                    index = {
                        **index,
                        "status": "stale",
                        "error": "The recording, retained analysis or index changed. Index again after review.",
                    }
            videos.append(
                {
                    "video_id": video["video_id"],
                    "title": video.get("title", "Recording"),
                    "analyses": [
                        {"analysis_id": run["analysis_id"], "created_at": run.get("created_at")}
                        for run in versions
                    ],
                    "index": self.public_status(index) if index else None,
                }
            )
        return {
            "videos": videos,
            "model": await blocking_media(check_model, self.settings),
            "busy": bool(self.task and not self.task.done()),
            "possibly_truncated": truncated,
            "note": NOTE,
        }

    async def enqueue(self, body, principal):
        tenant = self.identity(principal, write=True)
        async with self.enqueue_lock:
            if self.closing or (self.task and not self.task.done()):
                raise HTTPException(
                    409, "One recording is already indexing. Wait for it to finish before retrying."
                )
            async with video_mutation_lock(self.store, tenant, body.video_id):
                video = await self.video(tenant, body.video_id, principal, write=True)
                analysis = await self.source(tenant, video, body.analysis_id, principal, write=True)
                try:
                    samples = samples_for(video, analysis)
                    private_file(self.directory(tenant, body.video_id), "original.mp4")
                except ValueError as error:
                    raise HTTPException(409, str(error)) from error
                model = await blocking_media(check_model, self.settings)
                if not model["available"]:
                    raise HTTPException(503, model["reason"])
                row = await self.store.put(
                    tenant,
                    KIND,
                    body.video_id,
                    {
                        "video_id": body.video_id,
                        "analysis_id": body.analysis_id,
                        "status": "queued",
                        "progress": 0,
                        "sample_count": len(samples),
                        "model_id": MODEL_ID,
                        "source_key": source_key(video, analysis),
                        "requested_at": timestamp(),
                        "requested_by": principal.subject,
                        "consent_private_original": True,
                        "error": None,
                    },
                )
                self.task = asyncio.create_task(self.run(tenant, row, principal))
                return self.public_status(row)

    async def update(self, tenant, row, **changes):
        return await self.store.put(
            tenant, KIND, row["video_id"], {**row, **changes}, expected_revision=row["revision"]
        )

    async def run(self, tenant, row, principal):
        reader = None
        directory = self.directory(tenant, row["video_id"])
        # Share native decoder capacity with video analysis. The lifecycle lock prevents
        # archive/purge/configuration from racing an in-flight private read or final write.
        async with (
            video_processor_lock(self.store),
            self.model_lock,
            video_mutation_lock(self.store, tenant, row["video_id"]),
        ):
            try:
                video = await self.video(tenant, row["video_id"], principal, write=True)
                analysis = await self.source(
                    tenant, video, row["analysis_id"], principal, write=True
                )
                if source_key(video, analysis) != row["source_key"]:
                    raise ValueError("The source changed before indexing. Review and retry.")
                original = private_file(directory, "original.mp4")
                original_hash = await blocking_media(file_hash, original, 100 * 1024 * 1024)
                model = await self.runtime()
                reader = await blocking_media(FrameReader, original)
                selected = samples_for(video, analysis)
                row = await self.update(tenant, row, status="running")
                samples = []
                deadline = time.monotonic() + 300
                for selected_sample in selected:
                    if self.closing or time.monotonic() > deadline:
                        raise ValueError("Indexing stopped before completion. Review and retry.")
                    private_file(
                        directory, f"{row['analysis_id']}/{selected_sample['frame_index']}.jpg"
                    )
                    frame = await blocking_media(reader.sample, selected_sample["at_s"])
                    embedded = vector(await blocking_media(model.embed_image, frame))
                    samples.append({**selected_sample, "vector": embedded})
                    if len(samples) % 10 == 0:
                        row = await self.update(
                            tenant, row, progress=round(len(samples) / len(selected), 4)
                        )
                if original_hash != await blocking_media(file_hash, original, 100 * 1024 * 1024):
                    raise ValueError("The original changed during indexing. Review and retry.")
                # Recheck sources after native work, including out-of-process database changes.
                video = await self.video(tenant, row["video_id"], principal, write=True)
                analysis = await self.source(
                    tenant, video, row["analysis_id"], principal, write=True
                )
                if source_key(video, analysis) != row["source_key"]:
                    raise ValueError("The source changed during indexing. Review and retry.")
                await blocking_media(
                    write_index,
                    directory,
                    {
                        "version": 1,
                        "source_key": row["source_key"],
                        "model_id": MODEL_ID,
                        "original_sha256": original_hash,
                        "samples": samples,
                    },
                )
                await self.update(tenant, row, status="ready", progress=1, finished_at=timestamp())
            except Exception as error:
                with contextlib.suppress(OSError, ValueError):
                    private_file(directory, INDEX_NAME, exists=False).unlink(missing_ok=True)
                await self.update(
                    tenant,
                    row,
                    status="failed",
                    progress=0,
                    error=str(error)
                    if isinstance(error, ValueError)
                    else "Local indexing failed. Verify model and retained media, then retry.",
                    finished_at=timestamp(),
                )
            finally:
                if reader is not None:
                    await blocking_media(reader.close)

    async def ready_index(self, tenant, video_id, principal):
        video = await self.video(tenant, video_id, principal)
        row = await self.store.get(tenant, KIND, video_id)
        if not row or row.get("status") != "ready":
            raise HTTPException(409, "This recording does not have a completed search index")
        analysis = await self.source(tenant, video, row["analysis_id"], principal)
        try:
            directory = self.directory(tenant, video_id)
            payload = await blocking_media(read_index, directory)
            key = source_key(video, analysis)
            if (
                row.get("source_key") != key
                or payload.get("source_key") != key
                or payload.get("model_id") != MODEL_ID
            ):
                raise ValueError("Search source changed")
            expected = samples_for(video, analysis)
            positions = [
                {k: sample[k] for k in ("at_s", "frame_index")} for sample in payload["samples"]
            ]
            if positions != expected:
                raise ValueError("Search sample positions changed")
            original = private_file(directory, "original.mp4")
            if await blocking_media(file_hash, original, 100 * 1024 * 1024) != payload.get(
                "original_sha256"
            ):
                raise ValueError("Search source changed")
            return video, analysis, payload
        except (OSError, ValueError, KeyError, TypeError) as error:
            # Derived index validity is shared status, not a claim about source content.
            # Do not leave a corrupted/changed original looking searchable in the UI.
            with contextlib.suppress(WorkbenchConflict):
                await self.update(
                    tenant,
                    row,
                    status="stale",
                    progress=0,
                    error="The original, retained analysis or private index changed. Review and rebuild.",
                )
            raise HTTPException(
                409, "This index is stale or unavailable. Review the recording and index again."
            ) from error

    async def query(self, body, principal):
        tenant = self.identity(principal)
        if self.model_lock.locked():
            raise HTTPException(
                409,
                "Local semantic processing is busy. Retry after indexing or the current query completes.",
            )
        async with self.model_lock:
            model = await self.runtime()
            if body.sample:
                async with video_mutation_lock(self.store, tenant, body.sample.video_id):
                    _, analysis, payload = await self.ready_index(
                        tenant, body.sample.video_id, principal
                    )
                    sample = next(
                        (
                            sample
                            for sample in payload["samples"]
                            if sample["frame_index"] == body.sample.frame_index
                        ),
                        None,
                    )
                    if analysis["analysis_id"] != body.sample.analysis_id or not sample:
                        raise HTTPException(404, "The selected image is not in this retained index")
                    query_vector = sample["vector"]
            else:
                query_vector = vector(await blocking_media(model.embed_text, body.text))
            available, truncated = await self.candidates(principal, body.video_id)
            results, skipped, searched = [], 0, 0
            for original, _ in available:
                video_id = original["video_id"]
                async with video_mutation_lock(self.store, tenant, video_id):
                    try:
                        video, analysis, payload = await self.ready_index(
                            tenant, video_id, principal
                        )
                    except HTTPException as error:
                        if error.status_code not in {403, 404, 409}:
                            raise
                        skipped += 1
                        continue
                    searched += 1
                    for sample in payload["samples"]:
                        if (
                            body.sample
                            and video_id == body.sample.video_id
                            and sample["frame_index"] == body.sample.frame_index
                        ):
                            continue
                        try:
                            private_file(
                                self.directory(tenant, video_id),
                                f"{analysis['analysis_id']}/{sample['frame_index']}.jpg",
                            )
                        except ValueError:
                            continue
                        events = [
                            {
                                "event_id": event["event_id"],
                                "event_type": event.get("event_type", "Recorded event"),
                            }
                            for event in analysis.get("events", [])
                            if event.get("event_id")
                            and event.get("started_at_s", event.get("at_s", -1))
                            <= sample["at_s"]
                            <= event.get("end_s", -1)
                        ][:10]
                        results.append(
                            {
                                "video_id": video_id,
                                "analysis_id": analysis["analysis_id"],
                                "video_title": video.get("title", "Recording"),
                                "at_s": sample["at_s"],
                                "frame_index": sample["frame_index"],
                                "events": events,
                                "frame_url": f"/api/review/videos/{video_id}/frames/{analysis['analysis_id']}/{sample['frame_index']}",
                                "similarity": round(
                                    sum(
                                        a * b
                                        for a, b in zip(query_vector, sample["vector"], strict=True)
                                    ),
                                    5,
                                ),
                            }
                        )
            results.sort(
                key=lambda result: (-result["similarity"], result["video_id"], result["at_s"])
            )
            return {
                "results": results[: body.limit],
                "total_samples": len(results),
                "searched_recordings": searched,
                "skipped_recordings": skipped,
                "possibly_truncated": truncated,
                "model_id": MODEL_ID,
                "note": NOTE,
            }

    async def remove(self, video_id, principal):
        tenant = self.identity(principal, write=True)
        async with video_mutation_lock(self.store, tenant, video_id):
            video = await self.store.get(tenant, "video", video_id)
            if not video:
                raise HTTPException(404, "Recording not found")
            self.scopes(principal, video, write=True)
            row = await self.store.get(tenant, KIND, video_id)
            if row:
                if row.get("status") in {"queued", "running"}:
                    raise HTTPException(
                        409, "Wait for indexing to finish before removing its index"
                    )
                analysis = await self.store.get(tenant, "analysis", row["analysis_id"])
                if analysis:
                    self.scopes(principal, video, analysis, write=True)
                try:
                    private_file(self.directory(tenant, video_id), INDEX_NAME, exists=False).unlink(
                        missing_ok=True
                    )
                except ValueError as error:
                    raise HTTPException(
                        409, "Search index is not a regular private file"
                    ) from error
                await self.update(tenant, row, status="removed", progress=0, error=None)
            return {"removed": True}


def install_recorded_search_routes(app: FastAPI, settings, store, video_manager):
    manager = RecordedSearch(settings, store, video_manager)
    prefix = "/api/review/recorded-search"

    @app.get(prefix + "/catalog", tags=["recorded-search"])
    async def catalog(request: Request):
        return await manager.catalog(actor(request))

    @app.post(prefix + "/index", status_code=202, tags=["recorded-search"])
    async def index(body: IndexRequest, request: Request):
        return await manager.enqueue(body, actor(request))

    @app.delete(prefix + "/index/{video_id}", tags=["recorded-search"])
    async def remove(video_id: str, request: Request):
        return await manager.remove(video_id, actor(request))

    @app.post(prefix + "/query", tags=["recorded-search"])
    async def query(body: SearchRequest, request: Request):
        return await manager.query(body, actor(request))

    return manager
