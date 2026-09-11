"""Validation, escaping and honest statistics for the casework surface."""

from __future__ import annotations

import html
import json
import statistics
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


CASE_SOURCE_IDS = {
    "alert_id",
    "video_id",
    "analysis_id",
    "event_id",
    "annotation_set_id",
    "annotation_id",
    "evaluation_report_id",
}


class CaseCreate(StrictBody):
    alert_id: str | None = Field(default=None, min_length=1, max_length=200)
    video_id: str | None = Field(default=None, min_length=1, max_length=200)
    analysis_id: str | None = Field(default=None, min_length=1, max_length=200)
    event_id: str | None = Field(default=None, min_length=1, max_length=200)
    annotation_set_id: str | None = Field(default=None, min_length=1, max_length=200)
    annotation_id: str | None = Field(default=None, min_length=1, max_length=200)
    evaluation_report_id: str | None = Field(default=None, min_length=1, max_length=200)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    owner: str | None = Field(default=None, min_length=1, max_length=200)
    due_at: datetime | None = None
    summary: str = Field(default="", max_length=8000)
    priority: Literal["urgent", "high", "normal", "low"] = "normal"

    @field_validator("due_at")
    @classmethod
    def aware_date(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("due_at must include a timezone")
        return value

    @model_validator(mode="after")
    def one_source(self) -> CaseCreate:
        annotation_fields = (self.annotation_set_id, self.annotation_id, self.evaluation_report_id)
        if any(annotation_fields):
            if (
                self.alert_id
                or self.event_id
                or not all(
                    (self.video_id, self.analysis_id, self.annotation_set_id, self.annotation_id)
                )
            ):
                raise ValueError(
                    "Reviewer evidence requires video_id, analysis_id, annotation_set_id and annotation_id, without event_id or alert_id"
                )
            return self
        video_fields = (self.video_id, self.analysis_id, self.event_id)
        if self.alert_id and any(video_fields):
            raise ValueError("Choose either an alert or a recorded-video event")
        if not self.alert_id and not all(video_fields):
            raise ValueError("An alert_id or video_id, analysis_id and event_id are required")
        return self


class EvidenceAttach(StrictBody):
    expected_revision: int = Field(ge=1)
    alert_id: str | None = Field(default=None, min_length=1, max_length=200)
    video_id: str | None = Field(default=None, min_length=1, max_length=200)
    analysis_id: str | None = Field(default=None, min_length=1, max_length=200)
    event_id: str | None = Field(default=None, min_length=1, max_length=200)
    annotation_set_id: str | None = Field(default=None, min_length=1, max_length=200)
    annotation_id: str | None = Field(default=None, min_length=1, max_length=200)
    evaluation_report_id: str | None = Field(default=None, min_length=1, max_length=200)
    note: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def one_source(self):
        self.source_request()
        return self

    def source_request(self) -> CaseCreate:
        return CaseCreate(**self.model_dump(include=CASE_SOURCE_IDS))


class CasePatch(StrictBody):
    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["open", "investigating", "resolved"] | None = None
    owner: str | None = Field(default=None, min_length=1, max_length=200)
    due_at: datetime | None = None
    summary: str | None = Field(default=None, max_length=8000)
    verdict: Literal["unreviewed", "confirmed", "false_positive"] | None = None
    resolution_note: str | None = Field(default=None, min_length=1, max_length=8000)
    priority: Literal["urgent", "high", "normal", "low"] | None = None

    @field_validator("due_at")
    @classmethod
    def aware_date(cls, value: datetime | None) -> datetime | None:
        return CaseCreate.aware_date(value)

    @model_validator(mode="after")
    def no_null_required_fields(self) -> CasePatch:
        for name in ("title", "status", "summary", "verdict", "priority"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class NoteCreate(StrictBody):
    text: str = Field(min_length=1, max_length=8000)


class MissionLink(StrictBody):
    mission_id: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=1)


class SearchQuery(StrictBody):
    q: str = Field(default="", max_length=200)
    kind: Literal["entity", "event", "alert", "video", "video_event", "case", "site"] | None = None
    source_id: str | None = Field(default=None, max_length=200)
    zone_id: str | None = Field(default=None, max_length=200)
    since: datetime | None = None
    until: datetime | None = None
    limit: int = Field(default=50, ge=1, le=200)

    @field_validator("since", "until")
    @classmethod
    def aware_date(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Time filters must include a timezone")
        return value

    @model_validator(mode="after")
    def ordered(self) -> SearchQuery:
        if self.since and self.until and self.until < self.since:
            raise ValueError("until must be at or after since")
        return self


class SavedSearchCreate(StrictBody):
    name: str = Field(min_length=1, max_length=100)
    query: SearchQuery
    shared: bool = False


def iso_now() -> str:
    return datetime.now(UTC).isoformat()


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def escape_like(value: str) -> str:
    """Search strings are literal substrings, never user-provided SQL patterns."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def matches_result(row: dict[str, Any], query: SearchQuery) -> bool:
    if query.kind and row["kind"] != query.kind:
        return False
    if (
        query.q
        and query.q.casefold()
        not in " ".join(str(row.get(k) or "") for k in ("id", "title", "summary")).casefold()
    ):
        return False
    if query.zone_id and row.get("zone_id") != query.zone_id:
        return False
    if query.source_id and query.source_id not in (
        row.get("source_id"),
        row.get("video_id"),
        *(row.get("source_ids") or []),
    ):
        return False
    ts = parse_time(row.get("ts"))
    if query.since and (ts is None or ts < query.since):
        return False
    return not (query.until and (ts is None or ts > query.until))


def case_metrics(cases: list[dict[str, Any]], *, scan_limit: int) -> dict[str, Any]:
    counts = dict.fromkeys(
        ("confirmed", "false_positive", "unreviewed", "open", "investigating", "resolved"), 0
    )
    counts["total"] = len(cases)
    response: list[float] = []
    resolution: list[float] = []
    detector_counts = dict.fromkeys(("total", "confirmed", "false_positive", "unreviewed"), 0)
    reviewer_counts = dict(detector_counts)
    for case in cases:
        counts[case.get("verdict", "unreviewed")] += 1
        counts[case.get("status", "open")] += 1
        origin_counts = (
            reviewer_counts
            if (case.get("evidence") or {}).get("source") == "reviewer_annotation"
            else detector_counts
        )
        origin_counts["total"] += 1
        origin_counts[case.get("verdict", "unreviewed")] += 1
        start = parse_time(case.get("created_at"))
        for field, values in (("first_response_at", response), ("resolved_at", resolution)):
            end = parse_time(case.get(field))
            if start and end and end >= start:
                values.append((end - start).total_seconds())
    reviewed = counts["confirmed"] + counts["false_positive"]
    detector_reviewed = detector_counts["confirmed"] + detector_counts["false_positive"]
    return {
        "counts": counts,
        "reviewed_count": reviewed,
        "reviewed_precision": detector_counts["confirmed"] / detector_reviewed
        if detector_reviewed
        else None,
        "detector_counts": detector_counts,
        "detector_reviewed_count": detector_reviewed,
        "reviewer_annotation_counts": reviewer_counts,
        "reviewer_annotation_reviewed_count": reviewer_counts["confirmed"]
        + reviewer_counts["false_positive"],
        "median_response_seconds": statistics.median(response) if response else None,
        "response_sample_count": len(response),
        "median_resolution_seconds": statistics.median(resolution) if resolution else None,
        "resolution_sample_count": len(resolution),
        "recall": None,
        "missed_incidents": None,
        "scope": "visible_cases",
        "scan_limit": scan_limit,
        "possibly_truncated": len(cases) >= scan_limit,
        "limitations": [
            "Precision describes reviewed detector-originated cases only; reviewer-annotation-originated cases are excluded even when detector evidence is later attached. It is not detector accuracy across all footage.",
            "Reviewer-annotation case counts describe investigations, not additional false negatives or independent ground truth; creating or resolving a case never changes a frozen evaluation report.",
            "Case totals do not provide recall or missed-incident counts; those belong to explicitly scoped frozen evaluation reports.",
            "Response and resolution are measured from case creation, not the original incident time.",
        ],
    }


def printable_report(report: dict[str, Any]) -> str:
    """A self-contained printable report, with no active markup or external resources."""
    case = report["case"]

    def esc(value: Any) -> str:
        return html.escape(str(value if value is not None else "Unspecified"), quote=True)

    sections = []
    for label, data in (
        ("Evidence and provenance", case.get("evidence", {})),
        ("Source evidence timeline and attachments", case.get("evidence_items", [])),
        ("Investigation timeline", case.get("timeline", [])),
        ("Authored notes", case.get("notes", [])),
        ("Linked mission lifecycle", report.get("missions", [])),
        ("Decision approvals", report.get("decisions", [])),
    ):
        sections.append(
            f"<section><h2>{esc(label)}</h2><pre>{esc(json.dumps(data, indent=2, ensure_ascii=False, default=str))}</pre></section>"
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
        f"<title>{esc(case.get('title'))} — SIO case report</title>"
        "<style>body{font:14px/1.5 system-ui,sans-serif;margin:40px;max-width:1000px;color:#182321}"
        "h1{font-size:28px}h2{font-size:18px;margin-top:28px}pre{white-space:pre-wrap;overflow-wrap:anywhere;"
        "font:12px/1.5 ui-monospace,monospace;background:#f1f4f3;padding:16px}"
        "dt{font-weight:700}dd{margin:0 0 8px}section{break-inside:avoid}@media print{body{margin:12mm}}</style></head><body>"
        f"<p>SIO · Investigation report · {esc(report['exported_at'])}</p><h1>{esc(case.get('title'))}</h1>"
        "<dl>"
        + "".join(
            f"<dt>{esc(label)}</dt><dd>{esc(case.get(key))}</dd>"
            for label, key in (
                ("Case ID", "case_id"),
                ("Status", "status"),
                ("Verdict", "verdict"),
                ("Owner", "owner"),
                ("Due", "due_at"),
                ("Created by", "created_by"),
                ("Created", "created_at"),
                ("Resolved", "resolved_at"),
                ("Revision", "revision"),
            )
        )
        + "</dl>"
        f"<h2>Summary</h2><p>{esc(case.get('summary'))}</p>"
        + "".join(sections)
        + f"<p>Exported by {esc(report['exported_by'])}. Each evidence source is an immutable authored snapshot; linked missions and approvals reflect export time. Recording-relative seconds do not establish a shared clock between cameras. Media bytes are not embedded.</p></body></html>"
    )
