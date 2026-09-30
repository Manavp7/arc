"""Typed recorded-review and operations API, using the parent client's authenticated transport."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypeVar
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field


class Record(BaseModel):
    """Preserve additive server fields without losing the stable typed contract."""

    model_config = ConfigDict(extra="allow")


class Page(Record):
    next_cursor: str | None = None


class ReviewVideo(Record):
    video_id: str
    title: str
    duration_s: float
    status: str
    revision: int


class VideoPage(Page):
    videos: list[ReviewVideo]
    capabilities: dict[str, Any] = Field(default_factory=dict)


class VideoJob(Record):
    job_id: str
    video_id: str
    video_title: str
    analysis_id: str
    revision: int
    status: Literal[
        "queued", "running", "completed", "failed", "interrupted", "cancelled", "cancelling"
    ]
    progress: float
    attempt: int
    max_attempts: int
    queued_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    retry_of: str | None = None
    analysis_ids: list[str] = Field(default_factory=list)


class JobPage(Page):
    jobs: list[VideoJob]
    capabilities: dict[str, Any] = Field(default_factory=dict)


class RetainedAnalysis(Record):
    analysis_id: str
    status: str
    video_id: str | None = None
    created_at: str | None = None
    model: dict[str, Any] = Field(default_factory=dict)
    zones: list[dict[str, Any]] = Field(default_factory=list)


class AnalysisPage(Page):
    analyses: list[RetainedAnalysis]


class CaseRecord(Record):
    case_id: str
    title: str
    status: str
    revision: int
    owner: str | None = None
    summary: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)


class CasePage(Page):
    cases: list[CaseRecord]


class CaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    video_id: str | None = None
    analysis_id: str | None = None
    event_id: str | None = None
    alert_id: str | None = None
    annotation_set_id: str | None = None
    annotation_id: str | None = None
    evaluation_report_id: str | None = None
    title: str | None = None


class CaseUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    title: str | None = None
    owner: str | None = None
    due_at: str | None = None
    status: Literal["open", "investigating", "resolved"] | None = None
    summary: str | None = None
    verdict: Literal["unreviewed", "confirmed", "false_positive"] | None = None
    resolution_note: str | None = None


class SearchIndex(Record):
    video_id: str
    analysis_id: str
    status: str
    progress: float | None = None
    error: str | None = None


class SearchRecording(Record):
    video_id: str
    title: str
    analyses: list[dict[str, Any]] = Field(default_factory=list)
    index: SearchIndex | None = None


class SearchCatalog(Record):
    videos: list[SearchRecording]
    model: dict[str, Any]
    busy: bool
    possibly_truncated: bool = False
    note: str = ""


class RecordedSample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    video_id: str
    analysis_id: str
    frame_index: int = Field(ge=0)


class RecordedMatch(RecordedSample):
    model_config = ConfigDict(extra="allow")
    video_title: str
    at_s: float
    similarity: float
    frame_url: str
    events: list[dict[str, Any]] = Field(default_factory=list)


class RecordedSearchResult(Record):
    results: list[RecordedMatch]
    total_samples: int
    searched_recordings: int
    skipped_recordings: int = 0
    possibly_truncated: bool = False
    model_id: str
    note: str = ""


class RecordingClock(BaseModel):
    model_config = ConfigDict(extra="ignore")
    revision: int = Field(ge=0)
    camera_id: str = ""
    declared_by: str | None = Field(default=None, exclude=True)
    updated_at: str | None = Field(default=None, exclude=True)
    capture_started_at: str | None = None
    clock_offset_s: float = 0
    uncertainty_s: float | None = None
    note: str = ""


class TimelineRecording(Record):
    video_id: str
    title: str
    duration_s: float
    analysis_id: str | None = None
    clock: RecordingClock | None = None
    interval: dict[str, str] | None = None


class RecordingTimeline(Page):
    recordings: list[TimelineRecording]
    camera_ids: list[str] = Field(default_factory=list)
    note: str = ""


class ActivationPreview(Record):
    preview_id: str
    source_id: str
    action: Literal["activate", "rollback"]
    expires_at: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    changed_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    requires_fresh_observation: bool


class ActivationStatus(Record):
    activation_id: str | None = None
    source_id: str | None = None
    status: str | None = None
    message: str | None = None
    rollback_available: bool | None = None
    verified_at: str | None = None


class DeliveryHistory(Record):
    history_id: int | None = None
    kind: str
    at: str
    attempt: int
    actor: str | None = None
    reason: str | None = None
    error: str | None = None
    status_code: int | None = None


class AlertDelivery(Record):
    delivery_id: str
    alert_id: str
    status: Literal["pending", "sending", "delivered", "failed", "blocked"]
    attempts: int
    can_retry: bool
    history: list[DeliveryHistory] = Field(default_factory=list)
    history_next_cursor: str | None = None


class DeliveryPage(Page):
    deliveries: list[AlertDelivery]
    configured: bool
    destination: str | None = None
    max_attempts: int


class HistoryPage(Page):
    history: list[DeliveryHistory]


class CalibrationPreview(Record):
    preview_id: str
    operation: Literal["apply", "rollback"]
    setup_id: str
    setup_revision: int
    source_id: str
    expires_at: str
    previous_pose: dict[str, Any] | None = None
    proposed_pose: dict[str, Any] | None = None
    note: str


class FusionAcknowledgement(Record):
    acknowledged: bool
    acknowledged_at: str | None = None


class CalibrationStatus(Record):
    publication_id: str | None = None
    status: Literal["not_published", "pending_fusion", "applied", "rolled_back", "superseded"]
    source_id: str
    calibration_revision: int | None = None
    applied_at: str | None = None
    applied_by: str | None = None
    fusion: FusionAcknowledgement
    can_rollback: bool
    note: str


T = TypeVar("T", bound=BaseModel)


def _id(value: str) -> str:
    return quote(value, safe="")


class InvestigationClient:
    """Available as ``SioClient.investigations``; operations never run implicitly."""

    def __init__(self, request: Callable[..., Awaitable[Any]]) -> None:
        self._request = request

    async def _model(self, model: type[T], method: str, path: str, **kwargs: Any) -> T:
        return model.model_validate(await self._request(method, path, **kwargs))

    async def videos(self) -> VideoPage:
        return await self._model(VideoPage, "GET", "/api/review/videos")

    async def video(self, video_id: str) -> ReviewVideo:
        return await self._model(ReviewVideo, "GET", f"/api/review/videos/{_id(video_id)}")

    async def upload(
        self, content: bytes, filename: str, *, access_zone_id: str | None = None
    ) -> ReviewVideo:
        """Upload supplied MP4 bytes; access scope is separate from image polygons."""
        return await self._model(
            ReviewVideo,
            "POST",
            "/api/review/videos",
            params={"access_zone_id": access_zone_id},
            content=content,
            headers={"Content-Type": "video/mp4", "X-Filename": _id(filename)},
        )

    async def jobs(
        self,
        *,
        view: Literal["all", "active", "finished"] = "all",
        cursor: str | None = None,
        limit: int = 100,
    ) -> JobPage:
        return await self._model(
            JobPage,
            "GET",
            "/api/review/jobs",
            params={"view": view, "cursor": cursor, "limit": limit},
        )

    async def analyses(
        self, video_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> AnalysisPage:
        return await self._model(
            AnalysisPage,
            "GET",
            f"/api/review/videos/{_id(video_id)}/analyses",
            params={"cursor": cursor, "limit": limit},
        )

    async def analysis(self, video_id: str, analysis_id: str) -> RetainedAnalysis:
        return await self._model(
            RetainedAnalysis,
            "GET",
            f"/api/review/videos/{_id(video_id)}/analysis",
            params={"analysis_id": analysis_id},
        )

    async def cases(self) -> CasePage:
        return await self._model(CasePage, "GET", "/api/cases")

    async def case(self, case_id: str) -> CaseRecord:
        return await self._model(CaseRecord, "GET", f"/api/cases/{_id(case_id)}")

    async def create_case(self, body: CaseCreate) -> CaseRecord:
        return await self._model(
            CaseRecord, "POST", "/api/cases", json_body=body.model_dump(exclude_unset=True)
        )

    async def update_case(self, case_id: str, body: CaseUpdate) -> CaseRecord:
        return await self._model(
            CaseRecord,
            "PATCH",
            f"/api/cases/{_id(case_id)}",
            json_body=body.model_dump(exclude_unset=True),
        )

    async def search_catalog(self) -> SearchCatalog:
        return await self._model(SearchCatalog, "GET", "/api/review/recorded-search/catalog")

    async def index_recording(
        self, video_id: str, analysis_id: str, *, consent_private_original: bool
    ) -> SearchIndex:
        if consent_private_original is not True:
            raise ValueError(
                "Explicit consent to local processing of the private original is required"
            )
        return await self._model(
            SearchIndex,
            "POST",
            "/api/review/recorded-search/index",
            json_body={
                "video_id": video_id,
                "analysis_id": analysis_id,
                "consent_private_original": True,
            },
        )

    async def search_recordings(
        self,
        *,
        text: str | None = None,
        sample: RecordedSample | None = None,
        video_id: str | None = None,
        limit: int = 24,
    ) -> RecordedSearchResult:
        if bool(text and text.strip()) == bool(sample):
            raise ValueError("Provide exactly one nonempty text query or sample reference")
        return await self._model(
            RecordedSearchResult,
            "POST",
            "/api/review/recorded-search/query",
            json_body={
                "text": text.strip() if text else "",
                "sample": sample.model_dump() if sample else None,
                "video_id": video_id or "",
                "limit": limit,
            },
        )

    async def remove_search_index(self, video_id: str) -> bool:
        payload = await self._request(
            "DELETE", f"/api/review/recorded-search/index/{_id(video_id)}"
        )
        return bool(payload["removed"])

    async def timeline(
        self,
        *,
        camera_id: str | None = None,
        from_time: str | None = None,
        to_time: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> RecordingTimeline:
        return await self._model(
            RecordingTimeline,
            "GET",
            "/api/review/recording-timeline",
            params={
                "camera_id": camera_id,
                "from": from_time,
                "to": to_time,
                "cursor": cursor,
                "limit": limit,
            },
        )

    async def save_clock(self, video_id: str, clock: RecordingClock) -> RecordingClock:
        return await self._model(
            RecordingClock,
            "PUT",
            f"/api/review/videos/{_id(video_id)}/clock",
            json_body=clock.model_dump(),
        )

    async def activation_status(self, source_id: str) -> ActivationStatus:
        return await self._model(
            ActivationStatus, "GET", f"/api/sources/{_id(source_id)}/activation"
        )

    async def preview_activation(
        self, source_id: str, *, rollback: bool = False
    ) -> ActivationPreview:
        action = "rollback-preview" if rollback else "preview"
        return await self._model(
            ActivationPreview,
            "POST",
            f"/api/sources/{_id(source_id)}/activation/{action}",
            json_body={},
        )

    async def apply_activation(
        self, preview: ActivationPreview, *, timeout_s: int = 20
    ) -> ActivationStatus:
        suffix = "/rollback" if preview.action == "rollback" else ""
        return await self._model(
            ActivationStatus,
            "POST",
            f"/api/sources/{_id(preview.source_id)}/activation{suffix}",
            json_body={"preview_id": preview.preview_id, "timeout_s": timeout_s},
            timeout_s=90,
        )

    async def deliveries(
        self,
        *,
        status: str | None = None,
        alert_id: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> DeliveryPage:
        return await self._model(
            DeliveryPage,
            "GET",
            "/api/alert-deliveries",
            params={"status": status, "alert_id": alert_id, "cursor": cursor, "limit": limit},
        )

    async def delivery_history(
        self, delivery_id: str, *, cursor: str | None = None, limit: int = 20
    ) -> HistoryPage:
        return await self._model(
            HistoryPage,
            "GET",
            f"/api/alert-deliveries/{_id(delivery_id)}/history",
            params={"cursor": cursor, "limit": limit},
        )

    async def retry_delivery(self, delivery_id: str, reason: str) -> AlertDelivery:
        return await self._model(
            AlertDelivery,
            "POST",
            f"/api/alert-deliveries/{_id(delivery_id)}/retry",
            json_body={"reason": reason.strip()},
        )

    async def calibration_status(self, setup_id: str) -> CalibrationStatus:
        return await self._model(
            CalibrationStatus, "GET", f"/api/camera-setups/{_id(setup_id)}/calibration/status"
        )

    async def preview_calibration(
        self, setup_id: str, *, expected_revision: int, rollback: bool = False
    ) -> CalibrationPreview:
        action = "rollback-preview" if rollback else "preview"
        return await self._model(
            CalibrationPreview,
            "POST",
            f"/api/camera-setups/{_id(setup_id)}/calibration/{action}",
            json_body={"expected_revision": expected_revision},
        )

    async def apply_calibration(self, preview: CalibrationPreview) -> CalibrationStatus:
        return await self._model(
            CalibrationStatus,
            "POST",
            f"/api/camera-setups/{_id(preview.setup_id)}/calibration/{preview.operation}",
            json_body={
                "preview_id": preview.preview_id,
                "expected_revision": preview.setup_revision,
            },
        )
