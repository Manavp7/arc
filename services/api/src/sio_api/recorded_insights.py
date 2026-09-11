"""Search saved detections and retain reproducible recorded movement summaries."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import math
from copy import deepcopy
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import Field, field_validator, model_validator

from .case_helpers import StrictBody
from .cases import actor, review_video_available
from .observation_math import movement_summary, object_tracks
from .review_bookmarks import ReviewBookmarks
from .video_jobs import video_mutation_lock

KIND = "movement_report"
ANALYSIS_SCAN = 500
OBSERVATION_SCAN = 50000
VIDEO_SCAN = 20
REPORT_SCAN = 1000
MAX_PER_ANALYSIS = 20
MAX_PER_VIDEO = 100
NOTE = (
    "Tracks are local to an exact analysis, not personal identities. Defaults select the "
    "newest completed accessible run per recording. Times are recording offsets; unmatched "
    "or unsampled intervals do not establish absence."
)
Coordinate = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class CountingLine(StrictBody):
    name: str = Field(min_length=1, max_length=100)
    start: tuple[Coordinate, Coordinate]
    end: tuple[Coordinate, Coordinate]

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def nonzero(self):
        if math.dist(self.start, self.end) < 0.02:
            raise ValueError("The counting line must span at least 2% of the image")
        return self


class MovementConfiguration(StrictBody):
    line: CountingLine | None = None
    class_name: str = Field(default="", max_length=64)
    zone_id: str = Field(default="", max_length=64)
    min_confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)

    @field_validator("class_name", "zone_id", mode="before")
    @classmethod
    def trim_filter(cls, value):
        return value.strip() if isinstance(value, str) else value


class MovementRequest(StrictBody):
    analysis_id: str = Field(min_length=1, max_length=120)
    configuration: MovementConfiguration


class MovementCreate(MovementRequest):
    title: str = Field(min_length=1, max_length=120)

    @field_validator("title", mode="before")
    @classmethod
    def trim_title(cls, value):
        return value.strip() if isinstance(value, str) else value


class ObjectQuery(StrictBody):
    video_id: str = Field(default="", max_length=120)
    analysis_id: str = Field(default="", max_length=120)
    class_name: str = Field(default="", max_length=64)
    zone_id: str = Field(default="", max_length=64)
    min_confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    start_s: float = Field(default=0, ge=0, le=180, allow_inf_nan=False)
    end_s: float | None = Field(default=None, ge=0, le=180, allow_inf_nan=False)
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=OBSERVATION_SCAN)

    @model_validator(mode="after")
    def ordered(self):
        if (self.analysis_id or self.zone_id) and not self.video_id:
            raise ValueError("Choose a recording before selecting an analysis or image zone")
        if self.end_s is not None and self.end_s < self.start_s:
            raise ValueError("The end time must not precede the start time")
        return self


def report_lock(store):
    if not hasattr(store, "_movement_report_write_lock"):
        store._movement_report_write_lock = asyncio.Lock()
    return store._movement_report_write_lock


class RecordedInsights(ReviewBookmarks):
    """Uses the same source permissions and lifecycle locks as protected review notes."""

    async def video(self, tenant, video_id, principal, *, write=False):
        video = await self.store.get(tenant, "video", video_id)
        if not video:
            raise HTTPException(404, "Recording not found")
        self.scopes(principal, video, write=write)
        if not review_video_available(video):
            raise HTTPException(409, "This recording is unavailable for recorded insights")
        return video

    async def source(self, tenant, video, analysis_id, principal, *, write=False):
        analysis = await self.store.get(tenant, "analysis", analysis_id)
        if not analysis or analysis.get("video_id") != video["video_id"]:
            raise HTTPException(404, "Retained analysis not found for this recording")
        self.scopes(principal, video, analysis, write=write)
        self.complete(analysis)
        return analysis

    @staticmethod
    def complete(analysis):
        if analysis.get("status") != "completed":
            raise HTTPException(409, "Choose a completed retained analysis")
        frames = analysis.get("detections", [])
        if (
            not isinstance(frames, list)
            or len(frames) > 360
            or any(
                not isinstance(frame, dict)
                or not isinstance(frame.get("objects", []), list)
                or len(frame.get("objects", [])) > 32
                for frame in frames
            )
        ):
            raise HTTPException(409, "This analysis exceeds the recorded insight bounds")

    @staticmethod
    def zone(analysis, zone_id):
        if zone_id and not any(
            zone.get("zone_id") == zone_id for zone in analysis.get("zones", [])
        ):
            raise HTTPException(422, "Choose a zone from this retained analysis")

    async def candidates(self, principal, video_id=""):
        tenant = self.identity(principal)
        videos = (
            [await self.video(tenant, video_id, principal)]
            if video_id
            else await self.store.list(tenant, "video", limit=5000)
        )
        if hasattr(self.store, "list_analysis_metadata"):
            analyses = await self.store.list_analysis_metadata(tenant, limit=ANALYSIS_SCAN)
        else:
            analyses = await self.store.list(tenant, "analysis", limit=ANALYSIS_SCAN)
        # Stable chronology, rather than a changed video's latest-run pointer.
        analyses.sort(
            key=lambda row: (row.get("created_at", ""), row.get("analysis_id", "")), reverse=True
        )
        available = []
        for video in videos:
            if not review_video_available(video):
                continue
            try:
                self.scopes(principal, video)
            except HTTPException as error:
                if error.status_code != 403:
                    raise
                continue
            versions = []
            for analysis in analyses:
                if (
                    analysis.get("video_id") != video.get("video_id")
                    or analysis.get("status") != "completed"
                ):
                    continue
                try:
                    self.scopes(principal, video, analysis)
                    self.complete(analysis)
                    if (
                        isinstance(analysis.get("sample_count"), int)
                        and analysis["sample_count"] > 360
                    ):
                        continue
                except HTTPException as error:
                    if error.status_code not in {403, 409}:
                        raise
                    continue
                versions.append(analysis)
            if versions:
                available.append((video, versions))
        return available[:VIDEO_SCAN], len(analyses) >= ANALYSIS_SCAN or len(videos) >= 5000 or len(
            available
        ) > VIDEO_SCAN

    async def catalog(self, principal):
        available, truncated = await self.candidates(principal)
        return {
            "videos": [
                {
                    "video_id": video["video_id"],
                    "title": video.get("title", "Recording"),
                    "duration_s": video.get("duration_s", 0),
                    "analyses": [
                        {
                            "analysis_id": run["analysis_id"],
                            "created_at": run.get("created_at"),
                            "model": run.get("model", {}),
                            "zones": run.get("zones", []),
                            "classes": run.get("detected_classes")
                            if "detected_classes" in run
                            else sorted(
                                {
                                    str(obj["class_name"])
                                    for frame in run.get("detections", [])
                                    for obj in frame.get("objects", [])
                                    if isinstance(obj, dict) and obj.get("class_name")
                                }
                            ),
                        }
                        for run in versions
                    ],
                }
                for video, versions in available
            ],
            "possibly_truncated": truncated,
            "note": NOTE,
        }

    async def objects(self, query: ObjectQuery, principal):
        tenant = self.identity(principal)
        if query.analysis_id:
            video = await self.video(tenant, query.video_id, principal)
            pairs = [(video, [await self.source(tenant, video, query.analysis_id, principal)])]
            truncated = False
        else:
            pairs, truncated = await self.candidates(principal, query.video_id)
        results = []
        observations_scanned = 0
        filters = query.model_dump(exclude={"video_id", "analysis_id", "limit", "offset"})
        for original, versions in pairs:
            async with video_mutation_lock(self.store, tenant, original["video_id"]):
                # Recheck lifecycle and permissions after waiting for a competing writer.
                video = await self.video(tenant, original["video_id"], principal)
                analysis = await self.source(tenant, video, versions[0]["analysis_id"], principal)
                self.zone(analysis, query.zone_id)
                observation_count = sum(
                    len(frame.get("objects", [])) for frame in analysis.get("detections", [])
                )
                if observations_scanned + observation_count > OBSERVATION_SCAN:
                    truncated = True
                    break
                observations_scanned += observation_count
                results.extend(await asyncio.to_thread(object_tracks, video, analysis, **filters))
        results.sort(
            key=lambda row: (
                row["video_title"].casefold(),
                row["video_id"],
                row["first_s"],
                row["track_id"],
                row["class_name"],
            )
        )
        page = results[query.offset : query.offset + query.limit]
        return {
            "results": page,
            "count": len(results),
            "limit": query.limit,
            "offset": query.offset,
            "scan_truncated": truncated,
            "possibly_truncated": truncated or query.offset + len(page) < len(results),
            "note": NOTE,
        }

    async def calculate(self, video_id, body: MovementRequest, principal, *, save=False):
        tenant = self.identity(principal, write=save)
        video = await self.video(tenant, video_id, principal, write=save)
        analysis = await self.source(tenant, video, body.analysis_id, principal, write=save)
        self.zone(analysis, body.configuration.zone_id)
        config = body.configuration.model_dump(mode="json")
        summary = await asyncio.to_thread(movement_summary, video, analysis, config)
        return video, analysis, summary

    async def preview(self, video_id, body: MovementRequest, principal):
        tenant = self.identity(principal, write=True)
        async with video_mutation_lock(self.store, tenant, video_id):
            _, _, summary = await self.calculate(video_id, body, principal, save=True)
            return summary

    async def create_report(self, video_id, body: MovementCreate, principal):
        tenant = self.identity(principal, write=True)
        async with report_lock(self.store), video_mutation_lock(self.store, tenant, video_id):
            video, analysis, summary = await self.calculate(video_id, body, principal, save=True)
            signature = json.dumps(
                {"version": 1, "video_id": video_id, **body.model_dump(mode="json")},
                sort_keys=True,
                separators=(",", ":"),
            )
            report_id = "movement_" + hashlib.sha256(signature.encode()).hexdigest()[:32]
            existing = await self.store.get(tenant, KIND, report_id)
            if existing:
                self.scopes(principal, existing, write=True)
                return existing
            reports = await self.store.list(tenant, KIND, limit=REPORT_SCAN)
            if (
                len(reports) >= REPORT_SCAN
                or sum(row.get("video_id") == video_id for row in reports) >= MAX_PER_VIDEO
                or sum(row.get("analysis_id") == body.analysis_id for row in reports)
                >= MAX_PER_ANALYSIS
            ):
                raise HTTPException(
                    409,
                    "Movement report limit reached (20 per analysis, 100 per recording, 1000 per tenant)",
                )
            return await self.store.put(
                tenant,
                KIND,
                report_id,
                {
                    "report_id": report_id,
                    "title": body.title,
                    "video_id": video_id,
                    "video_title": video.get("title", "Recording"),
                    "analysis_id": body.analysis_id,
                    "created_by": principal.subject,
                    "configuration": body.configuration.model_dump(mode="json"),
                    "summary": summary,
                    "model": deepcopy(analysis.get("model", {})),
                    "zones": deepcopy(analysis.get("zones", [])),
                    "rules": deepcopy(analysis.get("rules", [])),
                    "source": "recorded_file",
                    "privacy": video.get("privacy"),
                },
                expected_revision=0,
            )

    async def reports(self, video_id, analysis_id, principal):
        tenant = self.identity(principal)
        async with video_mutation_lock(self.store, tenant, video_id):
            video = await self.video(tenant, video_id, principal)
            await self.source(tenant, video, analysis_id, principal)
            rows = await self.store.list(tenant, KIND, limit=REPORT_SCAN)
            visible = []
            for row in rows:
                if row.get("video_id") == video_id and row.get("analysis_id") == analysis_id:
                    try:
                        self.scopes(principal, row)
                    except HTTPException as error:
                        if error.status_code != 403:
                            raise
                        continue
                    visible.append(row)
            return {
                "reports": visible[:MAX_PER_ANALYSIS],
                "limit": MAX_PER_ANALYSIS,
                "possibly_truncated": len(rows) >= REPORT_SCAN or len(visible) > MAX_PER_ANALYSIS,
            }

    async def report(self, report_id, principal):
        tenant = self.identity(principal)
        row = await self.store.get(tenant, KIND, report_id)
        if not row:
            raise HTTPException(404, "Movement report not found")
        async with video_mutation_lock(self.store, tenant, row["video_id"]):
            video = await self.video(tenant, row["video_id"], principal)
            await self.source(tenant, video, row["analysis_id"], principal)
            self.scopes(principal, row)
            return row


def movement_csv(report):
    """One fixed schema includes provenance even when there are no sampled crossings."""
    output = io.StringIO(newline="")
    fields = [
        "kind",
        "report_id",
        "title",
        "video_id",
        "analysis_id",
        "model",
        "weights_sha256",
        "class_filter",
        "minimum_confidence",
        "zone_id",
        "line",
        "line_start_x",
        "line_start_y",
        "line_end_x",
        "line_end_y",
        "at_s",
        "from_s",
        "to_s",
        "direction",
        "track_id",
        "class_name",
        "count",
        "peak_count",
        "mean_sampled_occupancy",
        "a_to_b",
        "b_to_a",
        "total_crossings",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    config, summary, model = report["configuration"], report["summary"], report.get("model", {})
    line = config.get("line") or {}
    base = {
        "report_id": report["report_id"],
        "title": report["title"],
        "video_id": report["video_id"],
        "analysis_id": report["analysis_id"],
        "model": model.get("name", ""),
        "weights_sha256": model.get("weights_sha256", ""),
        "class_filter": config.get("class_name", ""),
        "minimum_confidence": config.get("min_confidence"),
        "zone_id": config.get("zone_id", ""),
        "line": line.get("name", ""),
        "line_start_x": line.get("start", [None, None])[0],
        "line_start_y": line.get("start", [None, None])[1],
        "line_end_x": line.get("end", [None, None])[0],
        "line_end_y": line.get("end", [None, None])[1],
    }

    def write(row):
        safe = {}
        for key, value in {**base, **row}.items():
            # Spreadsheet readers must treat user-supplied titles/labels as text.
            if isinstance(value, str) and (
                value.lstrip().startswith(("=", "+", "-", "@"))
                or value.startswith(("\t", "\r", "\n"))
            ):
                value = "'" + value
            safe[key] = value
        writer.writerow(safe)

    write(
        {
            "kind": "summary",
            "count": summary["sample_count"],
            "peak_count": summary["peak_count"],
            "mean_sampled_occupancy": summary["mean_sampled_occupancy"],
            "a_to_b": summary["totals"]["a_to_b"],
            "b_to_a": summary["totals"]["b_to_a"],
            "total_crossings": summary["totals"]["total"],
        }
    )
    for point in summary["occupancy"]:
        write({"kind": "sampled_occupancy", "at_s": point["at_s"], "count": point["count"]})
    for point in summary["crossings"]:
        write(
            {
                "kind": "observed_crossing",
                **{
                    key: point[key]
                    for key in ("at_s", "from_s", "to_s", "direction", "track_id", "class_name")
                },
            }
        )
    return output.getvalue()


def install_recorded_insight_routes(app: FastAPI, store: Any):
    manager = RecordedInsights(store)

    @app.get("/api/review/objects/catalog", tags=["recorded-insights"])
    async def catalog(request: Request):
        return await manager.catalog(actor(request))

    @app.get("/api/review/objects", tags=["recorded-insights"])
    async def objects(request: Request, query: Annotated[ObjectQuery, Query()]):
        return await manager.objects(query, actor(request))

    @app.get("/api/review/videos/{video_id}/movement", tags=["recorded-insights"])
    async def reports(
        video_id: str, request: Request, analysis_id: str = Query(min_length=1, max_length=120)
    ):
        return await manager.reports(video_id, analysis_id, actor(request))

    @app.post("/api/review/videos/{video_id}/movement/preview", tags=["recorded-insights"])
    async def preview(video_id: str, body: MovementRequest, request: Request):
        return await manager.preview(video_id, body, actor(request))

    @app.post("/api/review/videos/{video_id}/movement", tags=["recorded-insights"])
    async def create(video_id: str, body: MovementCreate, request: Request):
        return await manager.create_report(video_id, body, actor(request))

    @app.get("/api/review/movement/{report_id}/csv", tags=["recorded-insights"])
    async def export(report_id: str, request: Request):
        report = await manager.report(report_id, actor(request))
        return Response(
            movement_csv(report),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{report["report_id"]}.csv"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return manager
