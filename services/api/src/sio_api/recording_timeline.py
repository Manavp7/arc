"""Declared recording clocks; never substitute upload time for capture time."""

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import Field, field_validator, model_validator

from .case_helpers import StrictBody
from .recorded_insights import RecordedInsights
from .video_jobs import video_mutation_lock
from .workbench_store import decode_cursor, record_cursor

KIND = "recording_clock"


class RecordingClock(StrictBody):
    revision: int = Field(ge=0)
    camera_id: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9_. -]*$")
    capture_started_at: datetime | None = None
    clock_offset_s: float = Field(default=0, ge=-86400, le=86400, allow_inf_nan=False)
    uncertainty_s: float | None = Field(default=None, ge=0, le=86400, allow_inf_nan=False)
    note: str = Field(default="", max_length=1000)

    @field_validator("capture_started_at")
    @classmethod
    def timezone_required(cls, value):
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Capture time must include a timezone offset")
            if not 1970 <= value.year <= 2100:
                raise ValueError("Capture year must be between 1970 and 2100")
        return value

    @model_validator(mode="after")
    def documented_clock(self):
        if self.capture_started_at is not None and (not self.camera_id or not self.note):
            raise ValueError("A known capture clock requires a camera ID and its source or method")
        if self.capture_started_at is None and (
            self.clock_offset_s or self.uncertainty_s is not None
        ):
            raise ValueError("Set a capture time before adding clock correction or uncertainty")
        return self


def clock_interval(clock, duration_s):
    if not clock or not clock.get("capture_started_at"):
        return None
    captured = datetime.fromisoformat(clock["capture_started_at"].replace("Z", "+00:00"))
    start = captured.astimezone(UTC) + timedelta(seconds=clock["clock_offset_s"])
    return {"start": start.isoformat(), "end": (start + timedelta(seconds=duration_s)).isoformat()}


class RecordingTimeline(RecordedInsights):
    async def save_clock(self, video_id, body, principal):
        tenant = self.identity(principal, write=True)
        async with video_mutation_lock(self.store, tenant, video_id):
            video = await self.video(tenant, video_id, principal, write=True)
            previous = await self.store.get(tenant, KIND, video_id)
            if previous:
                self.scopes(principal, video, previous, write=True)
            payload = {
                **body.model_dump(mode="json", exclude={"revision"}),
                "video_id": video_id,
                "zones": video.get("zones", []),
                "evidence_zone_ids": video.get("evidence_zone_ids", []),
                "declared_by": principal.subject,
                "basis": "operator_declared",
            }
            return await self.store.put(tenant, KIND, video_id, payload, body.revision)

    async def timeline(self, principal, *, cursor="", limit=20, camera_id="", start=None, end=None):
        tenant = self.identity(principal)
        for bound in (start, end):
            if bound and (bound.tzinfo is None or bound.utcoffset() is None):
                raise HTTPException(422, "Timeline dates must include a timezone")
        if start and end and start > end:
            raise HTTPException(422, "Timeline start must not exceed end")
        if hasattr(self.store, "iter_records"):
            videos = self.store.iter_records(tenant, "video", cursor=cursor, active_only=True)
        else:

            async def memory_rows():
                records = await self.store.list(tenant, "video", limit=5000)
                records.sort(key=lambda row: (row["created_at"], row["record_id"]), reverse=True)
                after = decode_cursor(cursor) if cursor else None
                for row in records:
                    if not after or (row["created_at"], row["record_id"]) < after:
                        yield row

            videos = memory_rows()
        rows = []
        last_cursor = None
        has_more = False
        async for entry in videos:
            video_id = entry["video_id"]
            try:
                async with video_mutation_lock(self.store, tenant, video_id):
                    video = await self.video(tenant, video_id, principal)
                    clock = await self.store.get(tenant, KIND, video_id)
                    if clock:
                        self.scopes(principal, video, clock)
                    interval = clock_interval(clock, video["duration_s"])
                    if camera_id and (clock or {}).get("camera_id") != camera_id:
                        continue
                    if start and (not interval or datetime.fromisoformat(interval["end"]) < start):
                        continue
                    if end and (not interval or datetime.fromisoformat(interval["start"]) > end):
                        continue
                    if len(rows) == limit:
                        has_more = True
                        break
                    version = None
                    if hasattr(self.store, "iter_records"):
                        metadata = self.store.iter_records(
                            tenant, "analysis", video_id=video_id, statuses=("completed",)
                        )
                    else:

                        async def memory_analyses():
                            analyses = await self.store.list_analysis_metadata(tenant, limit=5000)
                            analyses.sort(
                                key=lambda row: (row.get("created_at", ""), row["analysis_id"]),
                                reverse=True,
                            )
                            for row in analyses:
                                yield row

                        metadata = memory_analyses()
                    async for analysis in metadata:
                        if (
                            analysis.get("video_id") != video_id
                            or analysis.get("status") != "completed"
                        ):
                            continue
                        try:
                            self.scopes(principal, video, analysis)
                        except HTTPException as error:
                            if error.status_code == 403:
                                continue
                            raise
                        version = analysis["analysis_id"]
                        break
                    rows.append(
                        {
                            "video_id": video_id,
                            "title": video["title"],
                            "duration_s": video["duration_s"],
                            "analysis_id": version,
                            "clock": clock,
                            "interval": interval,
                        }
                    )
                    last_cursor = record_cursor(entry)
            except HTTPException as error:
                if error.status_code not in {403, 404, 409}:
                    raise
        rows.sort(
            key=lambda item: (
                item["interval"] is None,
                item["interval"]["start"] if item["interval"] else "",
                item["video_id"],
            )
        )
        return {
            "recordings": rows,
            "possibly_truncated": has_more,
            "next_cursor": last_cursor if has_more else None,
            "camera_ids": sorted(
                {
                    row["clock"]["camera_id"]
                    for row in rows
                    if row.get("clock") and row["clock"].get("camera_id")
                }
            ),
            "note": "Capture clocks are operator declarations, not independently verified. "
            "Corrected time = declared capture time + clock correction. Unknown clocks stay separate; "
            "saved case evidence and manual comparison offsets remain unchanged.",
        }


def install_recording_timeline_routes(app: FastAPI, store):
    manager = RecordingTimeline(store)

    @app.get("/api/review/recording-timeline", tags=["recording-timeline"])
    async def timeline(
        request: Request,
        cursor: str = "",
        limit: int = Query(default=20, ge=1, le=100),
        camera_id: str = Query(default="", max_length=100),
        start: datetime | None = Query(default=None, alias="from"),
        end: datetime | None = Query(default=None, alias="to"),
    ):
        return await manager.timeline(
            request.state.principal,
            cursor=cursor,
            limit=limit,
            camera_id=camera_id,
            start=start,
            end=end,
        )

    @app.put("/api/review/videos/{video_id}/clock", tags=["recording-timeline"])
    async def save_clock(video_id: str, body: RecordingClock, request: Request):
        return await manager.save_clock(video_id, body, request.state.principal)

    return manager
