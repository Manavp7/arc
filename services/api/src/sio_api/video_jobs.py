"""Durable local recorded-video queue; a run is never reused after interruption."""

from __future__ import annotations

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from .review_models import AnalysisRequest, default_threshold, pin_profile, validate_artifact
from .video_rules import ReviewConfiguration

ACTIVE_JOB_STATES = {"queued", "running", "cancelling"}
MAX_ATTEMPTS = 3
MAX_PENDING = 50


def video_processor_lock(store: Any) -> asyncio.Lock:
    """One heavy video decoder/converter per local API, shared with evidence exports."""
    if not hasattr(store, "_video_processor_lock"):
        store._video_processor_lock = asyncio.Lock()
    return store._video_processor_lock


def video_mutation_lock(store: Any, tenant: str, video_id: str) -> asyncio.Lock:
    """Shared by reference writers and archive in the supported single API process."""
    if not hasattr(store, "_video_mutation_locks"):
        store._video_mutation_locks = {}
    return store._video_mutation_locks.setdefault((tenant, video_id), asyncio.Lock())


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


class VideoJobQueue(ABC):
    store: Any
    settings: Any
    closing: bool
    task: asyncio.Task | None
    lock: asyncio.Lock
    active_analysis: str | None

    @abstractmethod
    async def video(self, tenant: str, video_id: str) -> dict[str, Any]:
        """The host manager resolves a tenant-scoped retained video."""
        raise NotImplementedError

    @abstractmethod
    async def run(self, tenant: str, video: dict, analysis: dict):
        """The host manager executes and persists one bounded attempt."""
        raise NotImplementedError

    def init_queue(self) -> None:
        self.queue_lock = asyncio.Lock()
        self.known_tenants: set[str] = set()
        self.recovered_tenants: set[str] = set()
        self.recovery_lock = asyncio.Lock()

    async def new_analysis(self, tenant, video, configuration, actor, job_id):
        analysis_id = "ana_" + uuid.uuid4().hex
        return await self.store.put(
            tenant,
            "analysis",
            analysis_id,
            {
                "analysis_id": analysis_id,
                "job_id": job_id,
                "video_id": video["video_id"],
                "status": "queued",
                "progress": 0,
                "model": {"mode": "pending", "name": "Waiting for local processor"},
                "detections": [],
                "events": [],
                **configuration,
                "created_by": actor,
                "source": "recorded_file",
                "privacy": "full_frame_pixelation",
            },
            expected_revision=0,
        )

    async def jobs(self, tenant: str):
        return await self.store.list(tenant, "video_job", limit=5000)

    async def ensure_recovered(self, tenant: str):
        if tenant in self.recovered_tenants:
            return
        async with self.recovery_lock:
            if tenant in self.recovered_tenants:
                return
            for job in await self.jobs(tenant):
                if job["status"] not in {"running", "cancelling", "interrupted", "queued"}:
                    video = await self.store.get(tenant, "video", job["video_id"])
                    if (
                        video
                        and video.get("analysis_id") == job["analysis_id"]
                        and video.get("status") in {"analyzing", "queued", "running"}
                    ):
                        await self.store.put(
                            tenant,
                            "video",
                            video["video_id"],
                            {**video, "status": job["status"]},
                            expected_revision=video["revision"],
                        )
                    continue
                video = await self.store.get(tenant, "video", job["video_id"])
                analysis = await self.store.get(tenant, "analysis", job["analysis_id"])
                if not video or not analysis:
                    await self.store.put(
                        tenant,
                        "video_job",
                        job["job_id"],
                        {
                            **job,
                            "status": "failed",
                            "error": "Recorded input or analysis is missing",
                            "finished_at": timestamp(),
                        },
                        expected_revision=job["revision"],
                    )
                    continue
                if job["status"] == "queued" and analysis["status"] == "queued":
                    # Enqueue may have committed the job before the video pointer.
                    await self.store.put(
                        tenant,
                        "video",
                        video["video_id"],
                        {**video, "status": "queued", "analysis_id": analysis["analysis_id"]},
                        expected_revision=video["revision"],
                    )
                    continue
                if analysis["status"] in {"completed", "failed", "cancelled"}:
                    await self.store.put(
                        tenant,
                        "video_job",
                        job["job_id"],
                        {
                            **job,
                            "status": analysis["status"],
                            "progress": analysis.get("progress", 0),
                            "finished_at": timestamp(),
                        },
                        expected_revision=job["revision"],
                    )
                    await self.store.put(
                        tenant,
                        "video",
                        video["video_id"],
                        {**video, "status": analysis["status"]},
                        expected_revision=video["revision"],
                    )
                    continue
                cancelled = job["status"] == "cancelling"
                await self.store.put(
                    tenant,
                    "analysis",
                    analysis["analysis_id"],
                    {
                        **analysis,
                        "status": "cancelled" if cancelled else "interrupted",
                        "error": "Cancelled before restart"
                        if cancelled
                        else "The process stopped. This attempt is retained; recovery starts a new analysis version.",
                    },
                    expected_revision=analysis["revision"],
                )
                if cancelled or job["attempt"] >= MAX_ATTEMPTS:
                    state = "cancelled" if cancelled else "failed"
                    await self.store.put(
                        tenant,
                        "video_job",
                        job["job_id"],
                        {
                            **job,
                            "status": state,
                            "error": "Cancelled"
                            if cancelled
                            else "Automatic restart recovery exhausted three attempts; retry explicitly.",
                            "finished_at": timestamp(),
                        },
                        expected_revision=job["revision"],
                    )
                    await self.store.put(
                        tenant,
                        "video",
                        video["video_id"],
                        {**video, "status": state},
                        expected_revision=video["revision"],
                    )
                    continue
                replacement = await self.new_analysis(
                    tenant, video, job["configuration"], job["created_by"], job["job_id"]
                )
                await self.store.put(
                    tenant,
                    "video_job",
                    job["job_id"],
                    {
                        **job,
                        "status": "queued",
                        "analysis_id": replacement["analysis_id"],
                        "analysis_ids": [*job["analysis_ids"], replacement["analysis_id"]],
                        "attempt": job["attempt"] + 1,
                        "progress": 0,
                        "started_at": None,
                        "finished_at": None,
                        "error": None,
                        "queued_at": timestamp(),
                    },
                    expected_revision=job["revision"],
                )
                await self.store.put(
                    tenant,
                    "video",
                    video["video_id"],
                    {**video, "status": "queued", "analysis_id": replacement["analysis_id"]},
                    expected_revision=video["revision"],
                )
            self.recovered_tenants.add(tenant)
            self.known_tenants.add(tenant)
        self.kick()

    def kick(self):
        if not self.closing and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self.drain(), name="recorded-video-queue")
            self.task.add_done_callback(self.worker_finished)

    def worker_finished(self, task):
        if task.cancelled():
            self.recovered_tenants.clear()
            self.known_tenants.clear()
            return
        error = task.exception()
        if error:
            # A storage outage must not make a live API silently leave its queue stuck.
            # The next authenticated poll uses the same bounded restart recovery path.
            self.recovered_tenants.clear()
            self.known_tenants.clear()
            logging.getLogger(__name__).error(
                "Recorded-video worker stopped; recover on next access",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def enqueue(
        self, tenant, video_id, actor, *, retry=None, profile_request: AnalysisRequest | None = None
    ):
        await self.ensure_recovered(tenant)
        async with video_mutation_lock(self.store, tenant, video_id), self.queue_lock:
            video = await self.video(tenant, video_id)
            if video.get("archived_at"):
                raise HTTPException(409, "Restore this archived video before analysis")
            jobs = await self.jobs(tenant)
            if len(jobs) >= 5000:
                raise HTTPException(
                    409,
                    "The local job history limit is reached; administrator-managed history migration is required before adding jobs",
                )
            active = [job for job in jobs if job["status"] in ACTIVE_JOB_STATES]
            existing = next((job for job in active if job["video_id"] == video_id), None)
            if existing:
                if retry:
                    raise HTTPException(
                        409,
                        "This recording already has an active analysis; finish or cancel it before retrying",
                    )
                if profile_request is not None:
                    pinned = existing["configuration"].get("profile") or {}
                    threshold = (
                        profile_request.confidence_threshold
                        if profile_request.confidence_threshold is not None
                        else default_threshold(self.settings)
                    )
                    if (
                        pinned.get("requested_mode"),
                        pinned.get("confidence_threshold"),
                        pinned.get("sample_fps"),
                    ) != (profile_request.mode, threshold, profile_request.sample_fps):
                        raise HTTPException(
                            409,
                            "The active analysis has a different profile; finish or cancel it before selecting new settings",
                        )
                return await self.store.get(tenant, "analysis", existing["analysis_id"])
            if len(active) >= MAX_PENDING:
                raise HTTPException(409, "The local queue is full; wait for a job to finish")
            configuration = ReviewConfiguration.model_validate(
                retry["configuration"] if retry else video
            )
            if not any(rule.enabled for rule in configuration.rules):
                raise HTTPException(
                    422, "Save a zone and at least one enabled rule before analysis"
                )
            config = configuration.model_dump(mode="json")
            try:
                config["profile"] = (
                    await asyncio.to_thread(
                        validate_artifact, self.settings, retry["configuration"]["profile"]
                    )
                    if retry and retry["configuration"].get("profile")
                    else await asyncio.to_thread(
                        pin_profile, self.settings, profile_request or AnalysisRequest()
                    )
                )
            except ValueError as error:
                raise HTTPException(409 if retry else 422, str(error)) from error
            job_id = "job_" + uuid.uuid4().hex
            analysis = await self.new_analysis(tenant, video, config, actor, job_id)
            await self.store.put(
                tenant,
                "video_job",
                job_id,
                {
                    "job_id": job_id,
                    "video_id": video_id,
                    "video_title": video.get("title", "Recorded clip"),
                    "analysis_id": analysis["analysis_id"],
                    "analysis_ids": [analysis["analysis_id"]],
                    "status": "queued",
                    "progress": 0,
                    "attempt": 1,
                    "max_attempts": MAX_ATTEMPTS,
                    "queued_at": timestamp(),
                    "started_at": None,
                    "finished_at": None,
                    "created_by": actor,
                    "configuration": config,
                    "error": None,
                    "retry_of": retry["job_id"] if retry else None,
                },
                expected_revision=0,
            )
            await self.store.put(
                tenant,
                "video",
                video_id,
                {**video, "status": "queued", "analysis_id": analysis["analysis_id"]},
                expected_revision=video["revision"],
            )
        self.kick()
        return analysis

    async def update_job(self, tenant, job_id, **changes):
        async with self.queue_lock:
            job = await self.store.get(tenant, "video_job", job_id)
            return await self.store.put(
                tenant, "video_job", job_id, {**job, **changes}, expected_revision=job["revision"]
            )

    async def cancellation_requested(self, tenant, job_id):
        job = await self.store.get(tenant, "video_job", job_id)
        return job and job["status"] == "cancelling"

    async def drain(self):
        while not self.closing:
            async with self.queue_lock:
                pending = [
                    (tenant, job)
                    for tenant in sorted(self.known_tenants)
                    for job in await self.jobs(tenant)
                    if job["status"] == "queued"
                ]
                if not pending:
                    return
                tenant, job = min(
                    pending, key=lambda item: (item[1]["queued_at"], item[1]["job_id"])
                )
                job = await self.store.put(
                    tenant,
                    "video_job",
                    job["job_id"],
                    {**job, "status": "running", "started_at": timestamp()},
                    expected_revision=job["revision"],
                )
            video = await self.video(tenant, job["video_id"])
            analysis = await self.store.get(tenant, "analysis", job["analysis_id"])
            async with self.lock:
                self.active_analysis = analysis["analysis_id"]
                await self.run(tenant, video, analysis)

    async def cancel(self, tenant, job_id, revision):
        await self.ensure_recovered(tenant)
        initial = await self.store.get(tenant, "video_job", job_id)
        if not initial:
            raise HTTPException(404, "Job not found")
        async with video_mutation_lock(self.store, tenant, initial["video_id"]), self.queue_lock:
            job = await self.store.get(tenant, "video_job", job_id)
            if not job:
                raise HTTPException(404, "Job not found")
            if job["revision"] != revision:
                raise HTTPException(409, "Job changed; reload before cancelling")
            if job["status"] not in ACTIVE_JOB_STATES:
                raise HTTPException(409, "Only queued or running jobs can be cancelled")
            queued = job["status"] == "queued"
            job = await self.store.put(
                tenant,
                "video_job",
                job_id,
                {
                    **job,
                    "status": "cancelled" if queued else "cancelling",
                    "finished_at": timestamp() if queued else None,
                },
                expected_revision=revision,
            )
            if queued:
                analysis = await self.store.get(tenant, "analysis", job["analysis_id"])
                await self.store.put(
                    tenant,
                    "analysis",
                    job["analysis_id"],
                    {**analysis, "status": "cancelled", "error": "Cancelled before processing"},
                    expected_revision=analysis["revision"],
                )
                video = await self.video(tenant, job["video_id"])
                if video.get("analysis_id") == job["analysis_id"]:
                    await self.store.put(
                        tenant,
                        "video",
                        job["video_id"],
                        {**video, "status": "cancelled"},
                        expected_revision=video["revision"],
                    )
            return job

    async def retry_job(self, tenant, job_id, revision, actor):
        job = await self.store.get(tenant, "video_job", job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job["revision"] != revision:
            raise HTTPException(409, "Job changed; reload before retrying")
        if job["status"] not in {"failed", "cancelled", "interrupted"}:
            raise HTTPException(409, "Only failed, cancelled or interrupted jobs can be retried")
        analysis = await self.enqueue(tenant, job["video_id"], actor, retry=job)
        return await self.store.get(tenant, "video_job", analysis["job_id"])
