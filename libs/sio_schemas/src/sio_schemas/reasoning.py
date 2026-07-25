"""Reasoning contracts: explanation, event, forecast, decision, alert.

The :class:`Explanation` in this module is the load-bearing piece of PRD M20: it is
*mandatory* on copilot answers, events, alerts and decisions, so nothing SIO asserts can be
a black box.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from .base import Confidence, SioModel, TenantScoped, Timestamp, Traced, new_id, utc_now
from .enums import ActionType, AlertState, ApprovalState, EventType, EvidenceKind, Severity
from .geo import Geo


class EvidenceRef(SioModel):
    """A pointer to something a human can go and check."""

    kind: EvidenceKind
    ref: str = Field(description="Id, object-store key, or the query text for kind=query")
    ts: Timestamp | None = None
    source_id: str | None = None
    score: float | None = Field(default=None, description="Relevance/similarity, if ranked")
    note: str | None = None


class TimelineEntry(SioModel):
    """One beat in the story an explanation tells."""

    ts: Timestamp
    kind: str = Field(description="observation | detection | event | decision | action | note")
    summary: str
    ref: str | None = None


class Alternative(SioModel):
    """A hypothesis SIO considered and did not choose.

    Surfacing these is what separates "the system says X" from "the system considered Y and
    Z and here is why it preferred X".
    """

    hypothesis: str
    confidence: Confidence = 0.0
    why_not: str | None = None


class Explanation(SioModel):
    """The standard evidence bundle attached to every assertion (PRD M20, §8.1)."""

    summary: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)
    confidence: Confidence = 0.0
    sources: list[str] = Field(default_factory=list, description="Contributing sensors/systems")
    timeline: list[TimelineEntry] = Field(default_factory=list)
    related_entities: list[str] = Field(default_factory=list)
    alternatives: list[Alternative] = Field(default_factory=list)
    degraded: bool = Field(
        default=False,
        description="True when a fallback path produced this answer (e.g. LLM tool-calling failed)",
    )
    notes: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.evidence or self.timeline or self.related_entities)


class Event(TenantScoped, Traced):
    """Something meaningful happened (PRD M9). Append-only: events are never updated."""

    event_id: str = Field(default_factory=lambda: new_id("evt"))
    type: EventType
    severity: Severity = Severity.INFO
    entities: list[str] = Field(default_factory=list)
    geo: Geo | None = None
    zone_id: str | None = None
    ts: Timestamp = Field(default_factory=utc_now)
    detected_ts: Timestamp = Field(
        default_factory=utc_now,
        description="When SIO noticed, as distinct from when it happened (ts). Latency is auditable.",
    )
    evidence: list[EvidenceRef] = Field(default_factory=list)
    confidence: Confidence = 1.0
    explanation: Explanation = Field(default_factory=Explanation)
    rule_id: str | None = Field(default=None, description="Rule that fired, or None for anomalies")
    attributes: dict[str, Any] = Field(default_factory=dict)
    source_ids: list[str] = Field(default_factory=list)

    @property
    def detection_latency_s(self) -> float:
        return (self.detected_ts - self.ts).total_seconds()


class ForecastPoint(SioModel):
    """One predicted value with an interval. Point forecasts without intervals are a lie."""

    ts: Timestamp
    value: float
    lo: float | None = None
    hi: float | None = None

    @model_validator(mode="after")
    def _interval_ordered(self) -> ForecastPoint:
        if self.lo is not None and self.hi is not None and self.lo > self.hi:
            raise ValueError("forecast interval lo exceeds hi")
        return self


class Forecast(TenantScoped, Traced):
    """A prediction about the near future (PRD M10)."""

    forecast_id: str = Field(default_factory=lambda: new_id("fct"))
    target: str = Field(description="What is predicted: next_location, congestion, temperature, …")
    entity_id: str | None = None
    zone_id: str | None = None
    ts: Timestamp = Field(default_factory=utc_now, description="When the forecast was made")
    horizon_s: float = Field(gt=0)
    points: list[ForecastPoint] = Field(default_factory=list)
    geo_points: list[Geo] = Field(
        default_factory=list, description="Predicted positions, for trajectory forecasts"
    )
    model_name: str = Field(default="unknown")
    confidence: Confidence = 0.5
    interval_level: float = Field(default=0.9, gt=0.0, lt=1.0)
    explanation: Explanation = Field(default_factory=Explanation)


class DecisionOption(SioModel):
    """One candidate course of action, scored so the ranking is inspectable."""

    option_id: str = Field(default_factory=lambda: new_id("opt"))
    action: ActionType
    target_entity_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    score: float = Field(description="Higher is better; the objective value from the solver")
    expected_effect: str = Field(
        description="Plain-language effect, e.g. 'contains fire in ~4 min'"
    )
    expected_metrics: dict[str, float] = Field(
        default_factory=dict,
        description="Quantified effects: eta_s, coverage_pct, cost, risk_delta",
    )
    cost: float = 0.0
    risk: float = Field(default=0.0, ge=0.0, le=1.0)
    feasible: bool = True
    rejection_reason: str | None = None


class Decision(TenantScoped, Traced):
    """A recommendation with ranked options, a rationale, and an approval gate (PRD M12)."""

    decision_id: str = Field(default_factory=lambda: new_id("dec"))
    trigger_event: str | None = Field(default=None, description="Event id that prompted this")
    ts: Timestamp = Field(default_factory=utc_now)
    options: list[DecisionOption] = Field(default_factory=list)
    chosen: str | None = Field(default=None, description="option_id of the recommendation")
    rationale: str = ""
    expected_effect: str = ""
    confidence: Confidence = 0.5
    explanation: Explanation = Field(default_factory=Explanation)
    proposed_by: str = Field(default="decision", description="Service or agent that proposed it")
    approval: ApprovalState = ApprovalState.PENDING
    approved_by: str | None = None
    approved_ts: Timestamp | None = None
    executed_ts: Timestamp | None = None
    solver: str | None = Field(default=None, description="e.g. ortools-cpsat, ortools-vrp")

    @model_validator(mode="after")
    def _chosen_exists(self) -> Decision:
        if self.chosen and self.options:
            ids = {o.option_id for o in self.options}
            if self.chosen not in ids:
                raise ValueError(f"chosen option {self.chosen!r} is not among the options")
        return self

    @property
    def chosen_option(self) -> DecisionOption | None:
        return next((o for o in self.options if o.option_id == self.chosen), None)

    @property
    def is_actionable(self) -> bool:
        """Only an approved decision with a chosen option may touch the physical world."""
        return self.approval in (ApprovalState.APPROVED, ApprovalState.NOT_REQUIRED) and bool(
            self.chosen
        )


class Alert(TenantScoped, Traced):
    """A prioritised, deduplicated, escalatable notification (PRD M16)."""

    alert_id: str = Field(default_factory=lambda: new_id("alt"))
    title: str
    event_ids: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    severity: Severity = Severity.MEDIUM
    score: float = Field(default=0.0, ge=0.0, description="Priority score used for ordering")
    group_key: str = Field(
        description="Dedup/grouping key: type + entity/location bucket. Repeats fold together."
    )
    count: int = Field(default=1, ge=1, description="Number of folded-in occurrences")
    state: AlertState = AlertState.OPEN
    ts: Timestamp = Field(default_factory=utc_now)
    last_ts: Timestamp = Field(default_factory=utc_now)
    geo: Geo | None = None
    zone_id: str | None = None
    ack_by: str | None = None
    ack_ts: Timestamp | None = None
    escalated_ts: Timestamp | None = None
    resolved_ts: Timestamp | None = None
    assignee: str | None = None
    decision_ids: list[str] = Field(default_factory=list)
    explanation: Explanation = Field(default_factory=Explanation)
    urgency_reason: str | None = Field(
        default=None, description="Why this ranks where it does — shown next to the score"
    )
    escalation_reason: str | None = Field(
        default=None,
        description="Why this escalated. Separate from urgency_reason, which explains the SCORE.",
    )
    """Kept apart from `urgency_reason`, having learned the hard way that one field cannot serve both.

    Escalation used to overwrite the scoring explanation, and the result was an inbox where every row's
    justification for its priority read "unacknowledged for 21 min" — the escalation timer, which had
    nothing to do with why the alert scored what it scored. Worse, the line survived acknowledgement, so a
    row could say "acknowledged by operator" and "unacknowledged for 21 min" simultaneously.

    Two different facts about an alert. Two fields.
    """

    @property
    def is_open(self) -> bool:
        return self.state in (AlertState.OPEN, AlertState.ESCALATED)
