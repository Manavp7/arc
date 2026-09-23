"""Declared recording clocks; never substitute upload time for capture time."""

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, HTTPException, Request
from pydantic import Field, field_validator, model_validator

from .case_helpers import StrictBody
from .recorded_insights import RecordedInsights
from .video_jobs import video_mutation_lock

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
                "declared_by": principal.subject,
                "basis": "operator_declared",
            }
            return await self.store.put(tenant, KIND, video_id, payload, body.revision)

    async def timeline(self, principal):
        tenant = self.identity(principal)
        videos = await self.store.list(tenant, "video", limit=21)
        metadata = await self.store.list_analysis_metadata(tenant, limit=500)
        metadata.sort(
            key=lambda item: (item.get("created_at", ""), item["analysis_id"]), reverse=True
        )
        rows = []
        for entry in videos[:20]:
            video_id = entry["video_id"]
            try:
                async with video_mutation_lock(self.store, tenant, video_id):
                    video = await self.video(tenant, video_id, principal)
                    clock = await self.store.get(tenant, KIND, video_id)
                    if clock:
                        self.scopes(principal, video, clock)
                    version = None
                    for analysis in metadata:
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
                            "interval": clock_interval(clock, video["duration_s"]),
                        }
                    )
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
            "possibly_truncated": len(videos) > 20 or len(metadata) >= 500,
            "note": "Capture clocks are operator declarations, not independently verified. "
            "Corrected time = declared capture time + clock correction. Unknown clocks stay separate; "
            "saved case evidence and manual comparison offsets remain unchanged.",
        }


def install_recording_timeline_routes(app: FastAPI, store):
    manager = RecordingTimeline(store)

    @app.get("/api/review/recording-timeline", tags=["recording-timeline"])
    async def timeline(request: Request):
        return await manager.timeline(request.state.principal)

    @app.put("/api/review/videos/{video_id}/clock", tags=["recording-timeline"])
    async def save_clock(video_id: str, body: RecordingClock, request: Request):
        return await manager.save_clock(video_id, body, request.state.principal)

    return manager
