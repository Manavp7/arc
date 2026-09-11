"""Recorded-video workbench with tenant-scoped persistence and private originals."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import math
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from sio_core.guard import principal_of
from sio_core.tenancy import current_tenant

from .review_models import AnalysisRequest, model_catalog, pin_profile, validate_artifact
from .video_jobs import (
    ACTIVE_JOB_STATES,
    VideoJobQueue,
    timestamp,
    video_mutation_lock,
    video_processor_lock,
)
from .video_processing import (
    MAX_DURATION,
    MAX_FRAMES,
    PLAYBACK_FPS,
    SAMPLE_FPS,
    VideoProcessor,
    media_available,
    playback_frame_rate,
    prepare_derivatives,
    probe,
)
from .video_rules import ConfigurationUpdate, ReviewConfiguration, evaluate_rules
from .video_storage import install_storage_routes

MAX_BYTES = 100 * 1024 * 1024
MAX_VIDEOS = 20
PRIVATE_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


async def blocking_media(function, *args):
    """Finish an in-flight native decoder call before releasing its resource lock."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class VideoReviewManager(VideoJobQueue):
    def __init__(self, settings: Any, store: Any):
        self.settings = settings
        self.store = store
        self.root = (settings.data_dir / "video_review").resolve()
        self.lock = video_processor_lock(store)
        self.task: asyncio.Task | None = None
        self.active_analysis: str | None = None
        self.closing = False
        self.upload_slots = asyncio.Semaphore(2)
        self.init_queue()

    def capabilities(self):
        decoder = importlib.util.find_spec("cv2") is not None
        profiles = model_catalog(self.settings)
        return {
            "upload": media_available() and decoder,
            "motion_detection": decoder,
            "onnx_available": profiles["models"][1]["available"],
            "analysis_profiles": profiles,
            "max_bytes": MAX_BYTES,
            "max_duration_s": MAX_DURATION,
            "max_width": 1920,
            "max_height": 1080,
            "max_videos": MAX_VIDEOS,
            "sample_fps": SAMPLE_FPS,
            "privacy": "full_frame_pixelation",
            "note": "Requires local ffmpeg, ffprobe and OpenCV. Normal playback and evidence are pixelated; audio and original downloads are unavailable.",
        }

    def directory(self, tenant: str, video_id: str) -> Path:
        if not re.fullmatch(r"vid_[a-f0-9]{32}", video_id):
            raise HTTPException(404, "Video not found")
        tenant_key = hashlib.sha256(tenant.encode()).hexdigest()
        if (self.root / tenant_key).is_symlink() or (
            self.root / tenant_key / video_id
        ).is_symlink():
            raise HTTPException(404, "Video not found")
        directory = (self.root / tenant_key / video_id).resolve()
        if not directory.is_relative_to(self.root):
            raise HTTPException(404, "Video not found")
        return directory

    async def video(self, tenant: str, video_id: str):
        self.directory(tenant, video_id)
        video = await self.store.get(tenant, "video", video_id)
        if video is None or video.get("purged_at"):
            raise HTTPException(404, "Video not found")
        return video

    async def playback_metadata(self, tenant: str, video: dict):
        if video.get("playback_fps"):
            return video
        try:
            directory = self.directory(tenant, video["video_id"])
            path = (directory / "playback.mp4").resolve()
            if not path.is_relative_to(directory):
                return video
            stat = path.stat()
            rate = await blocking_media(
                playback_frame_rate, str(path), stat.st_mtime_ns, stat.st_size
            )
            return {**video, "playback_fps": rate} if rate is not None else video
        except OSError:
            return video

    async def start(self):
        self.closing = False
        tenants = {self.settings.tenant_id}
        if hasattr(self.store, "list_tenants"):
            tenants.update(await self.store.list_tenants("video_job"))
        for tenant in sorted(tenants):
            await self.ensure_recovered(tenant)
        # Upgrade pre-queue runs without inventing a resumable checkpoint.
        for video in await self.store.list(self.settings.tenant_id, "video", limit=500):
            if not video.get("purged_at"):
                await self.recover(self.settings.tenant_id, video)

    async def close(self):
        self.closing = True
        if self.task:
            await self.task

    async def recover(self, tenant: str, video: dict):
        await self.ensure_recovered(tenant)
        async with video_mutation_lock(self.store, tenant, video["video_id"]):
            video = await self.video(tenant, video["video_id"])
            analysis_id = video.get("analysis_id")
            if not analysis_id or analysis_id == self.active_analysis:
                return video
            analysis = await self.store.get(tenant, "analysis", analysis_id)
            if (
                analysis
                and not analysis.get("job_id")
                and analysis.get("status") in ("queued", "running")
            ):
                analysis.update(
                    status="interrupted",
                    error="The process stopped before this run completed. Analyze again to create a new version.",
                )
                await self.store.put(
                    tenant,
                    "analysis",
                    analysis_id,
                    analysis,
                    expected_revision=analysis["revision"],
                )
                video = await self.store.put(
                    tenant,
                    "video",
                    video["video_id"],
                    {**video, "status": "interrupted"},
                    expected_revision=video["revision"],
                )
            return video

    async def upload(self, request: Request):
        if not (await blocking_media(self.capabilities))["upload"]:
            raise HTTPException(503, "Recorded video needs local ffmpeg, ffprobe and OpenCV")
        if request.headers.get("content-type", "").split(";")[0] != "video/mp4":
            raise HTTPException(415, "Upload the MP4 bytes with Content-Type video/mp4")
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            raise HTTPException(400, "Invalid upload length") from None
        if length > MAX_BYTES:
            raise HTTPException(413, "Video exceeds 100 MiB")
        tenant = current_tenant()
        async with self.upload_slots, video_mutation_lock(self.store, tenant, "uploads"):
            stored = await self.store.list(tenant, "video", limit=5000)
            if (
                len(stored) >= 5000
                or len([item for item in stored if not item.get("purged_at")]) >= MAX_VIDEOS
            ):
                raise HTTPException(409, "This prototype is limited to 20 stored videos per tenant")
            video_id = "vid_" + uuid.uuid4().hex
            directory = self.directory(tenant, video_id)
            directory.mkdir(parents=True, mode=0o700)
            saved = False
            try:
                total = 0
                original = directory / "original.mp4"
                with original.open("xb") as handle:
                    original.chmod(0o600)
                    try:
                        async with asyncio.timeout(60):
                            async for chunk in request.stream():
                                total += len(chunk)
                                if total > MAX_BYTES:
                                    raise HTTPException(413, "Video exceeds 100 MiB")
                                writing = asyncio.create_task(
                                    asyncio.to_thread(handle.write, chunk)
                                )
                                try:
                                    await asyncio.shield(writing)
                                except asyncio.CancelledError:
                                    await writing
                                    raise
                    except TimeoutError:
                        raise HTTPException(
                            408, "Upload did not finish within 60 seconds"
                        ) from None
                if total < 12:
                    raise HTTPException(422, "The upload is empty or is not an MP4")
                with original.open("rb") as handle:
                    if handle.read(12)[4:8] != b"ftyp":
                        raise HTTPException(422, "The upload is not an MP4 container")
                try:
                    async with self.lock:
                        probing = asyncio.create_task(asyncio.to_thread(probe, original))
                        try:
                            info = await asyncio.shield(probing)
                        except asyncio.CancelledError:
                            await probing
                            raise
                        conversion = asyncio.create_task(
                            asyncio.to_thread(prepare_derivatives, directory, info)
                        )
                        try:
                            await asyncio.shield(conversion)
                        except asyncio.CancelledError:
                            await conversion
                            raise
                except ValueError as error:
                    raise HTTPException(422, str(error)) from error
                except Exception as error:
                    raise HTTPException(
                        422,
                        "The clip could not be decoded or converted within the processing limit",
                    ) from error
                title = Path(unquote(request.headers.get("x-filename", "Recorded clip.mp4"))).name
                title = (
                    "".join(char for char in title if char.isprintable())[:120] or "Recorded clip"
                )
                base = f"/api/review/videos/{video_id}"
                record = await self.store.put(
                    tenant,
                    "video",
                    video_id,
                    {
                        "video_id": video_id,
                        "id": video_id,
                        "title": title,
                        **info,
                        "playback_fps": PLAYBACK_FPS,
                        "bytes": total,
                        "status": "ready",
                        "media_url": base + "/media",
                        "poster_url": base + "/poster",
                        "privacy": "full_frame_pixelation",
                        "source": "recorded_file",
                        "zones": [],
                        "rules": [],
                        "analysis_id": None,
                        "created_by": principal_of(request).subject,
                    },
                    expected_revision=0,
                )
                saved = True
                return record
            finally:
                if not saved:
                    await asyncio.to_thread(shutil.rmtree, directory, True)

    async def analyze(
        self, tenant: str, video_id: str, actor: str, profile_request: AnalysisRequest | None = None
    ):
        return await self.enqueue(tenant, video_id, actor, profile_request=profile_request)

    async def run(self, tenant: str, video: dict, analysis: dict):
        processor = None
        analysis_id, video_id = analysis["analysis_id"], video["video_id"]
        directory = self.directory(tenant, video_id)
        frames_dir = directory / analysis_id
        deadline = time.monotonic() + 300
        try:
            if await self.cancellation_requested(tenant, analysis["job_id"]):
                analysis.update(status="cancelled", error="Cancelled before processing")
                return
            if self.closing:
                analysis.update(status="interrupted", error="Process stopped before processing")
                return
            async with video_mutation_lock(self.store, tenant, video_id):
                current = await self.video(tenant, video_id)
                if current.get("analysis_id") == analysis_id:
                    await self.store.put(
                        tenant,
                        "video",
                        video_id,
                        {**current, "status": "analyzing"},
                        expected_revision=current["revision"],
                    )
            frames_dir.mkdir(mode=0o700)
            if not analysis.get("profile"):
                # Pre-profile queued jobs have no historical pin. Resolve once, then retain it
                # for any subsequent retry/recovery instead of consulting settings again.
                analysis["profile"] = await blocking_media(
                    pin_profile, self.settings, AnalysisRequest()
                )
                job = await self.store.get(tenant, "video_job", analysis["job_id"])
                await self.update_job(
                    tenant,
                    analysis["job_id"],
                    configuration={**job["configuration"], "profile": analysis["profile"]},
                )
            profile = await blocking_media(validate_artifact, self.settings, analysis["profile"])
            processor = await blocking_media(
                VideoProcessor, directory / "original.mp4", self.settings, profile
            )
            analysis.update(status="running", model=processor.model)
            configuration = ReviewConfiguration.model_validate(analysis)
            if processor.model["mode"] == "motion" and any(
                rule.enabled and rule.target_class not in ("any", "motion")
                for rule in configuration.rules
            ):
                raise ValueError(
                    "Motion mode cannot classify people or vehicles. Select target class any/motion, or install working ONNX weights."
                )
            analysis = await self.store.put(tenant, "analysis", analysis_id, analysis)
            sample_fps = profile["sample_fps"]
            count = min(MAX_FRAMES, max(1, math.ceil(video["duration_s"] * sample_fps)))
            for index in range(count):
                if await self.cancellation_requested(tenant, analysis["job_id"]):
                    analysis.update(status="cancelled", error="Cancelled by an operator")
                    break
                if self.closing:
                    analysis.update(
                        status="interrupted",
                        error="Process stopped; analyze again to create a new version",
                    )
                    break
                if time.monotonic() > deadline:
                    raise ValueError(
                        "Analysis exceeded the five-minute processing budget; this run is incomplete"
                    )
                at = index / sample_fps
                objects = await blocking_media(processor.sample, at, frames_dir / f"{index}.jpg")
                if objects is None:
                    raise ValueError(f"Video decoding stopped at {at:.1f}s; this run is incomplete")
                analysis["detections"].append(
                    {
                        "at_s": at,
                        "frame_index": index,
                        "frame_url": f"/api/review/videos/{video_id}/frames/{analysis_id}/{index}",
                        "objects": objects,
                    }
                )
                analysis["progress"] = round((index + 1) / count, 4)
                if index % 10 == 0:
                    analysis = await self.store.put(tenant, "analysis", analysis_id, analysis)
                    await self.update_job(tenant, analysis["job_id"], progress=analysis["progress"])
            else:
                analysis.update(
                    status="completed", progress=1, events=evaluate_rules(analysis, configuration)
                )
        except asyncio.CancelledError:
            analysis.update(
                status="interrupted", error="Process stopped; analyze again to create a new version"
            )
        except Exception as error:
            analysis.update(
                status="failed",
                error=str(error)
                if isinstance(error, ValueError)
                else "Video analysis failed; verify the file and local model dependencies",
            )
        finally:
            if processor:
                with contextlib.suppress(Exception):
                    await blocking_media(processor.close)
            try:
                async with video_mutation_lock(self.store, tenant, video_id), self.queue_lock:
                    job = await self.store.get(tenant, "video_job", analysis["job_id"])
                    if job["status"] == "cancelling":
                        analysis.update(
                            status="cancelled", events=[], error="Cancelled by an operator"
                        )
                    await self.store.put(tenant, "analysis", analysis_id, analysis)
                    await self.store.put(
                        tenant,
                        "video_job",
                        job["job_id"],
                        {
                            **job,
                            "status": analysis["status"],
                            "progress": analysis["progress"],
                            "error": analysis.get("error"),
                            "finished_at": timestamp(),
                        },
                        expected_revision=job["revision"],
                    )
                    current = await self.video(tenant, video_id)
                    if current.get("analysis_id") == analysis_id:
                        await self.store.put(
                            tenant,
                            "video",
                            video_id,
                            {**current, "status": analysis["status"]},
                            expected_revision=current["revision"],
                        )
            finally:
                self.active_analysis = None


def install_video_routes(app: FastAPI, settings: Any, store: Any) -> VideoReviewManager:
    manager = VideoReviewManager(settings, store)
    prefix = "/api/review/videos"

    @app.get(prefix, tags=["video-review"])
    async def list_videos():
        tenant = current_tenant()
        await manager.ensure_recovered(tenant)
        videos = [
            item
            for item in await store.list(tenant, "video", limit=5000)
            if not item.get("archived_at") and not item.get("purged_at")
        ][:MAX_VIDEOS]
        return {
            "videos": [
                await manager.playback_metadata(tenant, await manager.recover(tenant, item))
                for item in videos
            ],
            "capabilities": await blocking_media(manager.capabilities),
        }

    @app.post(prefix, status_code=201, tags=["video-review"])
    async def upload(request: Request):
        return await manager.upload(request)

    @app.get(prefix + "/{video_id}", tags=["video-review"])
    async def video(video_id: str):
        tenant = current_tenant()
        return await manager.playback_metadata(
            tenant, await manager.recover(tenant, await manager.video(tenant, video_id))
        )

    @app.put(prefix + "/{video_id}/configuration", tags=["video-review"])
    async def configure(video_id: str, body: ConfigurationUpdate):
        tenant = current_tenant()
        async with video_mutation_lock(store, tenant, video_id):
            record = await manager.video(tenant, video_id)
            if record["revision"] != body.revision:
                raise HTTPException(409, "Video configuration changed; reload before saving")
            if record.get("archived_at"):
                raise HTTPException(409, "Restore this archived video before configuring it")
            if any(
                job["video_id"] == video_id and job["status"] in ACTIVE_JOB_STATES
                for job in await manager.jobs(tenant)
            ):
                raise HTTPException(
                    409, "Wait for or cancel the queued analysis before changing configuration"
                )
            config = body.model_dump(mode="json", exclude={"revision"})
            return await store.put(
                tenant, "video", video_id, {**record, **config}, expected_revision=body.revision
            )

    @app.post(prefix + "/{video_id}/analyze", status_code=202, tags=["video-review"])
    async def analyze(video_id: str, request: Request, body: AnalysisRequest | None = None):
        return await manager.analyze(
            current_tenant(), video_id, principal_of(request).subject, body
        )

    @app.get(prefix + "/{video_id}/analysis", tags=["video-review"])
    async def analysis(video_id: str, analysis_id: str | None = None):
        tenant = current_tenant()
        if analysis_id is not None and not re.fullmatch(r"ana_[a-f0-9]{32}", analysis_id):
            raise HTTPException(404, "Analysis not found")
        record = await manager.recover(tenant, await manager.video(tenant, video_id))
        selected_id = analysis_id if analysis_id is not None else record.get("analysis_id")
        result = await store.get(tenant, "analysis", selected_id or "")
        if result is None or result.get("video_id") != video_id:
            raise HTTPException(404, "Analysis not found for this video")
        return result

    @app.post(prefix + "/{video_id}/preview", tags=["video-review"])
    async def preview(video_id: str, body: ReviewConfiguration):
        result = await analysis(video_id)
        if result["status"] != "completed":
            raise HTTPException(409, "Finish an analysis before previewing rule changes")
        if result["model"]["mode"] == "motion" and any(
            rule.enabled and rule.target_class not in ("any", "motion") for rule in body.rules
        ):
            raise HTTPException(422, "Motion analysis supports target class any or motion only")
        return {
            "preview": True,
            "analysis_id": result["analysis_id"],
            "model": result["model"],
            "events": evaluate_rules(result, body),
        }

    async def media_path(video_id: str, filename: str):
        tenant = current_tenant()
        await manager.video(tenant, video_id)
        path = (manager.directory(tenant, video_id) / filename).resolve()
        if not path.is_relative_to(manager.directory(tenant, video_id)) or not path.is_file():
            raise HTTPException(404, "Processed media is unavailable")
        return path

    @app.get(prefix + "/{video_id}/media", tags=["video-review"])
    async def media(video_id: str):
        return FileResponse(
            await media_path(video_id, "playback.mp4"),
            media_type="video/mp4",
            headers=PRIVATE_HEADERS,
        )

    @app.get(prefix + "/{video_id}/poster", tags=["video-review"])
    async def poster(video_id: str):
        return FileResponse(
            await media_path(video_id, "poster.jpg"),
            media_type="image/jpeg",
            headers=PRIVATE_HEADERS,
        )

    @app.get(prefix + "/{video_id}/frames/{analysis_id}/{frame_index}", tags=["video-review"])
    async def frame(video_id: str, analysis_id: str, frame_index: int):
        if not re.fullmatch(r"ana_[a-f0-9]{32}", analysis_id) or not 0 <= frame_index < MAX_FRAMES:
            raise HTTPException(404, "Evidence frame not found")
        record = await store.get(current_tenant(), "analysis", analysis_id)
        if record is None or record.get("video_id") != video_id:
            raise HTTPException(404, "Evidence frame not found")
        return FileResponse(
            await media_path(video_id, f"{analysis_id}/{frame_index}.jpg"),
            media_type="image/jpeg",
            headers=PRIVATE_HEADERS,
        )

    install_storage_routes(app, manager)
    return manager
