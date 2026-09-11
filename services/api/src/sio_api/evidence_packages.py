"""Bounded case evidence packages from protected derivatives and authoritative records."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import subprocess
import uuid
import zipfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import Field, model_validator

from sio_core.tenancy import current_tenant

from .case_evidence import case_evidence_items, case_evidence_zones, recorded_evidence
from .case_helpers import StrictBody, iso_now, printable_report
from .cases import Casework, actor, allowed, need, review_video_available
from .video_jobs import video_mutation_lock, video_processor_lock
from .video_processing import probe
from .workbench_store import WorkbenchConflict

MAX_CLIP_SECONDS = 60
MAX_PACKAGE_BYTES = 100 * 1024 * 1024
RECOVERY_SCAN_LIMIT = 5000


class PackageAnnotation(StrictBody):
    at_s: float = Field(ge=0, le=180, allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=2000)


class PackageRequest(StrictBody):
    case_id: str = Field(min_length=1, max_length=120)
    attachment_id: str = Field(default="original", pattern=r"^(original|evidence_[a-f0-9]{24})$")
    start_s: float = Field(ge=0, le=180, allow_inf_nan=False)
    end_s: float = Field(gt=0, le=180, allow_inf_nan=False)
    annotations: list[PackageAnnotation] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def bounded_clip(self):
        if not 0.1 <= self.end_s - self.start_s <= MAX_CLIP_SECONDS:
            raise ValueError("Choose a clip between 0.1 and 60 seconds")
        if any(not self.start_s <= note.at_s <= self.end_s for note in self.annotations):
            raise ValueError("Annotation timestamps must fall inside the selected clip")
        return self


def private_video_directory(settings: Any, tenant: str, video_id: str) -> Path:
    if not re.fullmatch(r"vid_[a-f0-9]{32}", video_id):
        raise HTTPException(404, "Recording not found")
    root = (Path(settings.data_dir) / "video_review").resolve()
    directory = (root / hashlib.sha256(tenant.encode()).hexdigest() / video_id).resolve()
    if not directory.is_relative_to(root):
        raise HTTPException(404, "Recording not found")
    return directory


def build_package(
    source: Path, directory: Path, record: dict[str, Any], report: dict[str, Any]
) -> dict[str, Any]:
    """Runs in a bounded worker thread; FFmpeg inputs and output names are server-controlled."""
    dimensions = probe(source)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    clip = directory / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-y",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(source),
            "-ss",
            str(record["start_s"]),
            "-t",
            str(record["end_s"] - record["start_s"]),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-vf",
            f"scale=16:16:flags=area,scale={dimensions['width']}:{dimensions['height']}:flags=neighbor,setsar=1,fps=15",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-fs",
            str(MAX_PACKAGE_BYTES // 2),
            str(clip),
        ],
        capture_output=True,
        timeout=90,
        check=True,
    )
    if not clip.is_file() or clip.stat().st_size == 0:
        raise ValueError("The selected clip could not be encoded")
    # Limit reached output can be a valid but shortened MP4. Verify the requested interval survived.
    checked = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(clip),
        ],
        capture_output=True,
        timeout=10,
        check=True,
    )
    actual_duration = float(json.loads(checked.stdout)["format"]["duration"])
    expected_duration = record["end_s"] - record["start_s"]
    if abs(actual_duration - expected_duration) > 0.2:
        raise ValueError("Encoded clip duration differs from the requested interval")
    annotations = {
        "time_basis": "original_recording_seconds",
        "clip_start_s": record["start_s"],
        "clip_end_s": record["end_s"],
        "authored_by": record["created_by"],
        "authored_at": record["created_at"],
        "annotations": [
            {**note, "clip_at_s": round(note["at_s"] - record["start_s"], 3)}
            for note in record["annotations"]
        ],
        "note": "Annotations are operator-authored observations, not independently verified findings.",
    }
    (directory / "case.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    (directory / "case.html").write_text(printable_report(report), encoding="utf-8")
    (directory / "annotations.json").write_text(
        json.dumps(annotations, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    filenames = ["clip.mp4", "case.json", "case.html", "annotations.json"]
    manifest = {
        "format_version": 1,
        "package_id": record["package_id"],
        "case_id": record["case_id"],
        "case_revision": report["case"]["revision"],
        "video_id": record["video_id"],
        "analysis_id": record["analysis_id"],
        "event_id": record.get("event_id", report["case"].get("event_id")),
        "annotation_set_id": record.get("annotation_set_id"),
        "annotation_id": record.get("annotation_id"),
        "evaluation_report_id": record.get("evaluation_report_id"),
        "attachment_id": record.get("attachment_id", "original"),
        "created_by": record["created_by"],
        "created_at": record["created_at"],
        "clip_start_s": record["start_s"],
        "clip_end_s": record["end_s"],
        "encoded_duration_s": actual_duration,
        "privacy": "full_frame_pixelation",
        "audio": "removed",
        "files": [
            {
                "path": name,
                "bytes": (directory / name).stat().st_size,
                "sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest(),
            }
            for name in filenames
        ],
        "integrity_note": "Hashes identify these exact package files; they are not a digital signature or independent authenticity certification.",
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    archive = directory / "package.zip"
    if sum((directory / name).stat().st_size for name in filenames) > MAX_PACKAGE_BYTES:
        raise ValueError("Evidence package exceeds 100 MiB")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=4) as bundle:
        for name in [*filenames, "manifest.json"]:
            bundle.write(directory / name, arcname=name)
    for path in directory.iterdir():
        path.chmod(0o600)
    return {
        "manifest": manifest,
        "bytes": archive.stat().st_size,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }


class EvidencePackageManager:
    def __init__(
        self, settings: Any, store: Any, pool: Any, resource_lock: asyncio.Lock | None = None
    ):
        self.settings, self.store = settings, store
        self.casework = Casework(store, pool)
        self.root = (Path(settings.data_dir) / "evidence_packages").resolve()
        self.lock = resource_lock or video_processor_lock(store)
        self.admission_lock = asyncio.Lock()
        self.active: set[str] = set()
        self.tasks: set[asyncio.Task] = set()
        self.closing = False
        self.recovery_by_tenant: dict[str, dict[str, Any]] = {}

    async def start(self):
        """Reconcile interrupted exports before notification baseline capture, without a UI visit."""
        async with self.admission_lock:
            tenants = (
                await self.store.list_tenants("evidence_package")
                if hasattr(self.store, "list_tenants")
                else [self.settings.tenant_id]
            )
            for tenant in tenants:
                records = await self.store.list(
                    tenant, "evidence_package", limit=RECOVERY_SCAN_LIMIT
                )
                recovered = 0
                for record in records:
                    if (
                        record.get("status") in ("queued", "building")
                        and record["package_id"] not in self.active
                    ):
                        result = await self.load(tenant, record["package_id"])
                        recovered += result["status"] == "interrupted"
                status = {
                    "scanned": len(records),
                    "scan_limit": RECOVERY_SCAN_LIMIT,
                    "possibly_truncated": len(records) >= RECOVERY_SCAN_LIMIT,
                    "interrupted": recovered,
                }
                self.recovery_by_tenant[tenant] = status
                if status["possibly_truncated"]:
                    logging.getLogger(__name__).warning(
                        "Evidence package startup recovery reached its per-tenant scan limit of %s; older pending exports may require an explicit visit",
                        RECOVERY_SCAN_LIMIT,
                    )

    def directory(self, tenant: str, package_id: str) -> Path:
        if not re.fullmatch(r"pkg_[a-f0-9]{32}", package_id):
            raise HTTPException(404, "Evidence package not found")
        return self.root / hashlib.sha256(tenant.encode()).hexdigest() / package_id

    async def load(self, tenant: str, package_id: str):
        self.directory(tenant, package_id)
        record = await self.store.get(tenant, "evidence_package", package_id)
        if record is None:
            raise HTTPException(404, "Evidence package not found")
        if record["status"] in ("queued", "building") and package_id not in self.active:
            try:
                record = await self.store.put(
                    tenant,
                    "evidence_package",
                    package_id,
                    {
                        **record,
                        "status": "interrupted",
                        "finished_at": iso_now(),
                        "error": "The API restarted before this package completed. Create a new package to retry.",
                    },
                    expected_revision=record["revision"],
                )
            except WorkbenchConflict:
                latest = await self.store.get(tenant, "evidence_package", package_id)
                if latest is None:
                    raise HTTPException(404, "Evidence package not found") from None
                record = latest
        return record

    async def create(self, body: PackageRequest, request: Request):
        async with self.admission_lock:
            return await self._create(body, request)

    async def _create(self, body: PackageRequest, request: Request):
        principal = actor(request)
        need(principal, "case.write")
        need(principal, "review.read")
        need(principal, "media.read")
        if self.closing:
            raise HTTPException(503, "Evidence builder is shutting down")
        if len(self.active) >= 3:
            raise HTTPException(409, "Three evidence packages are already waiting or building")
        tenant = current_tenant()
        case = await self.casework.detail(body.case_id, principal)
        selected = next(
            (
                item
                for item in case_evidence_items(case)
                if item["attachment_id"] == body.attachment_id
            ),
            None,
        )
        if selected is None:
            raise HTTPException(404, "Evidence attachment not found in this case")
        video_id, analysis_id = selected.get("video_id"), selected.get("analysis_id")
        if not video_id or not analysis_id or not recorded_evidence(selected):
            raise HTTPException(422, "Choose recorded-video evidence attached to this case")
        async with video_mutation_lock(self.store, tenant, video_id):
            video = await self.store.get(tenant, "video", video_id)
            analysis = await self.store.get(tenant, "analysis", analysis_id)
            if not review_video_available(video):
                raise HTTPException(
                    409, "Restore an available protected recording before packaging evidence"
                )
            if (
                not analysis
                or analysis.get("video_id") != video_id
                or analysis.get("status") != "completed"
            ):
                raise HTTPException(404, "The case's exact completed analysis is unavailable")
            if (selected.get("evidence") or {}).get("source") == "reviewer_annotation":
                annotation = selected["evidence"].get("annotation") or {}
                frozen = selected["evidence"].get("annotation_set") or {}
                if (
                    not selected.get("annotation_id")
                    or not selected.get("annotation_set_id")
                    or annotation.get("annotation_id") != selected["annotation_id"]
                    or frozen.get("annotation_set_id") != selected["annotation_set_id"]
                    or frozen.get("video_id") != video_id
                    or annotation not in frozen.get("annotations", [])
                ):
                    raise HTTPException(422, "The case's frozen reviewer annotation is unavailable")
            elif not any(
                event.get("event_id") == selected.get("event_id")
                and event.get("video_id", video_id) == video_id
                and event.get("analysis_id", analysis_id) == analysis_id
                for event in analysis.get("events", [])
            ):
                raise HTTPException(404, "The case event is absent from its recorded analysis")
            if video.get("privacy") != "full_frame_pixelation":
                raise HTTPException(409, "A protected pixelated recording is required")
            if body.end_s > float(video.get("duration_s") or 0):
                raise HTTPException(422, "The selected clip extends past the recording duration")
            source = private_video_directory(self.settings, tenant, video_id) / "playback.mp4"
            if not source.is_file():
                raise HTTPException(404, "The protected video rendition is unavailable")
            package_id = "pkg_" + uuid.uuid4().hex
            self.active.add(package_id)
            try:
                record = await self.store.put(
                    tenant,
                    "evidence_package",
                    package_id,
                    {
                        **body.model_dump(mode="json"),
                        "package_id": package_id,
                        "video_id": video_id,
                        "analysis_id": analysis_id,
                        "event_id": selected.get("event_id"),
                        "annotation_set_id": selected.get("annotation_set_id"),
                        "annotation_id": selected.get("annotation_id"),
                        "evaluation_report_id": selected.get("evaluation_report_id"),
                        "case_revision": case["revision"],
                        "evidence_title": selected.get("title")
                        or (selected.get("evidence") or {}).get("video", {}).get("title"),
                        "case_evidence_refs": [
                            {
                                key: item.get(key)
                                for key in (
                                    "attachment_id",
                                    "video_id",
                                    "analysis_id",
                                    "event_id",
                                    "alert_id",
                                    "annotation_set_id",
                                    "annotation_id",
                                    "evaluation_report_id",
                                )
                            }
                            for item in case_evidence_items(case)
                        ],
                        "case_evidence_zone_ids": sorted(case_evidence_zones(case)),
                        "status": "queued",
                        "created_by": principal.subject,
                        "created_at": iso_now(),
                        "privacy": "full_frame_pixelation",
                        "case_title": case["title"],
                        "required_actions": ["case.read", "review.read", "media.read"]
                        + (["mission.read"] if case.get("missions") else [])
                        + (["decisions.read"] if case.get("decisions") else []),
                        "included_zone_ids": list(
                            {
                                row["zone_id"]
                                for row in case.get("missions", [])
                                if row.get("zone_id")
                            }
                        ),
                        "download_url": f"/api/evidence-packages/{package_id}/download",
                    },
                    expected_revision=0,
                )
            except BaseException:
                self.active.discard(package_id)
                raise
        report = {
            "format_version": 1,
            "exported_at": iso_now(),
            "exported_by": principal.subject,
            "case": {
                key: value for key, value in case.items() if key not in ("missions", "decisions")
            },
            "missions": case.get("missions", []),
            "decisions": case.get("decisions", []),
        }
        task = asyncio.create_task(
            self.run(tenant, record, source, report), name="evidence-" + package_id
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return record

    async def run(self, tenant: str, record: dict, source: Path, report: dict):
        package_id = record["package_id"]
        directory = self.directory(tenant, package_id)
        try:
            async with self.lock:
                record = await self.store.put(
                    tenant,
                    "evidence_package",
                    package_id,
                    {**record, "status": "building", "started_at": iso_now()},
                    expected_revision=record["revision"],
                )
                result = await asyncio.to_thread(build_package, source, directory, record, report)
                await self.store.put(
                    tenant,
                    "evidence_package",
                    package_id,
                    {**record, **result, "status": "ready", "finished_at": iso_now()},
                    expected_revision=record["revision"],
                )
        except Exception as error:
            await asyncio.to_thread(shutil.rmtree, directory, True)
            record = await self.store.get(tenant, "evidence_package", package_id)
            if record:
                await self.store.put(
                    tenant,
                    "evidence_package",
                    package_id,
                    {
                        **record,
                        "status": "failed",
                        "error": "Package could not be built. Check local FFmpeg availability and recording storage, then create a new package.",
                        "error_type": type(error).__name__,
                    },
                    expected_revision=record["revision"],
                )
        finally:
            self.active.discard(package_id)

    async def close(self):
        async with self.admission_lock:
            self.closing = True
            pending = list(self.tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def install_evidence_package_routes(
    app: FastAPI, settings: Any, store: Any, pool: Any, resource_lock: asyncio.Lock | None = None
):
    manager = EvidencePackageManager(settings, store, pool, resource_lock)

    def package_scope(record, principal):
        return (
            all(
                allowed(principal, action)
                for action in record.get(
                    "required_actions", ["case.read", "review.read", "media.read"]
                )
            )
            and all(
                allowed(principal, action, zone)
                for zone in record.get("case_evidence_zone_ids", [])
                for action in ("case.read", "review.read", "media.read")
            )
            and all(
                allowed(principal, "mission.read", zone)
                for zone in record.get("included_zone_ids", [])
            )
        )

    async def visible(package_id: str, request: Request):
        principal = actor(request)
        record = await manager.load(current_tenant(), package_id)
        await manager.casework.load(record["case_id"], principal)
        if not package_scope(record, principal):
            raise HTTPException(
                403, "Your current access does not cover the captured package evidence"
            )
        for action in record.get("required_actions", ["case.read", "review.read", "media.read"]):
            need(principal, action)
        for zone_id in record.get("included_zone_ids", []):
            need(principal, "mission.read", zone_id)
        return record

    @app.get("/api/evidence-packages", tags=["evidence-packages"])
    async def packages(request: Request):
        principal = actor(request)
        cases = {row["case_id"] for row in await manager.casework.visible_cases(principal)}
        records = await store.list(current_tenant(), "evidence_package", limit=500)
        return {
            "recovery": manager.recovery_by_tenant.get(current_tenant()),
            "packages": [
                await manager.load(current_tenant(), row["package_id"])
                for row in records
                if row["case_id"] in cases and package_scope(row, principal)
            ],
        }

    @app.post("/api/evidence-packages", tags=["evidence-packages"], status_code=202)
    async def create(body: PackageRequest, request: Request):
        return await manager.create(body, request)

    @app.get("/api/evidence-packages/{package_id}", tags=["evidence-packages"])
    async def detail(package_id: str, request: Request):
        return await visible(package_id, request)

    @app.get("/api/evidence-packages/{package_id}/download", tags=["evidence-packages"])
    async def download(package_id: str, request: Request):
        record = await visible(package_id, request)
        path = manager.directory(current_tenant(), package_id) / "package.zip"
        if record["status"] != "ready":
            raise HTTPException(409, "Wait for a ready evidence package")
        if not path.is_file():
            raise HTTPException(404, "Package file is unavailable")
        return FileResponse(
            path,
            media_type="application/zip",
            filename=f"evidence-{package_id}.zip",
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    return manager
