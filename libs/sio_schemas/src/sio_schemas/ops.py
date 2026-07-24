"""Operational contracts: missions, workflows, simulations, audit, webhooks."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from .base import Confidence, SioModel, TenantScoped, Timestamp, Traced, new_id, utc_now
from .enums import EvidenceKind, MissionState, Role, RunStatus
from .geo import Geo
from .reasoning import EvidenceRef, Explanation


class MissionObjective(SioModel):
    objective_id: str = Field(default_factory=lambda: new_id("obj"))
    description: str
    zone_id: str | None = None
    geo: Geo | None = None
    due_ts: Timestamp | None = None
    done: bool = False
    progress: float = Field(default=0.0, ge=0.0, le=1.0)


class Mission(TenantScoped, Traced):
    """A multi-user operation: objectives, assigned resources, live status (PRD M17)."""

    mission_id: str = Field(default_factory=lambda: new_id("msn"))
    name: str
    description: str | None = None
    state: MissionState = MissionState.DRAFT
    objectives: list[MissionObjective] = Field(default_factory=list)
    assignees: list[str] = Field(default_factory=list, description="User ids")
    resources: list[str] = Field(
        default_factory=list, description="Entity ids: drones, patrols, docks"
    )
    commander: str | None = None
    zone_id: str | None = None
    geo: Geo | None = None
    created_ts: Timestamp = Field(default_factory=utc_now)
    updated_ts: Timestamp = Field(default_factory=utc_now)
    started_ts: Timestamp | None = None
    completed_ts: Timestamp | None = None
    event_ids: list[str] = Field(default_factory=list)
    alert_ids: list[str] = Field(default_factory=list)
    decision_ids: list[str] = Field(default_factory=list)
    comms: list[str] = Field(default_factory=list, description="Append-only comms log entries")

    @property
    def progress(self) -> float:
        if not self.objectives:
            return 1.0 if self.state == MissionState.COMPLETED else 0.0
        return sum(o.progress for o in self.objectives) / len(self.objectives)


class WorkflowStep(SioModel):
    """One step of a playbook, with enough detail for the UI to show live progress."""

    step_id: str
    name: str
    status: RunStatus = RunStatus.PENDING
    started_ts: Timestamp | None = None
    finished_ts: Timestamp | None = None
    attempts: int = Field(default=0, ge=0)
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None

    @property
    def duration_s(self) -> float | None:
        if self.started_ts and self.finished_ts:
            return (self.finished_ts - self.started_ts).total_seconds()
        return None


class WorkflowRun(TenantScoped, Traced):
    """An execution of a response playbook (PRD M15)."""

    run_id: str = Field(default_factory=lambda: new_id("wfr"))
    playbook: str
    status: RunStatus = RunStatus.PENDING
    trigger_event: str | None = None
    steps: list[WorkflowStep] = Field(default_factory=list)
    started_ts: Timestamp = Field(default_factory=utc_now)
    finished_ts: Timestamp | None = None
    runner: str = Field(default="temporal", description="temporal | inline")
    external_id: str | None = Field(
        default=None, description="Temporal workflow id, when applicable"
    )
    entity_ids: list[str] = Field(default_factory=list)
    explanation: Explanation = Field(default_factory=Explanation)

    @property
    def progress(self) -> float:
        if not self.steps:
            return 0.0
        done = sum(1 for s in self.steps if s.status == RunStatus.COMPLETED)
        return done / len(self.steps)


class SimulationRun(TenantScoped, Traced):
    """A what-if scenario execution (PRD M11)."""

    run_id: str = Field(default_factory=lambda: new_id("sim"))
    scenario: str = Field(
        description="gate_closure | fire_spread | dock_breakdown | flood_level | …"
    )
    params: dict[str, Any] = Field(default_factory=dict)
    status: RunStatus = RunStatus.PENDING
    started_ts: Timestamp = Field(default_factory=utc_now)
    finished_ts: Timestamp | None = None
    seeded_from_ts: Timestamp | None = Field(
        default=None, description="World-model instant the simulation was seeded from"
    )
    results: dict[str, Any] = Field(default_factory=dict)
    kpi_deltas: dict[str, float] = Field(
        default_factory=dict,
        description="Projected change per KPI, e.g. {'throughput_per_h': -12.5}",
    )
    impacted_entities: list[str] = Field(default_factory=list)
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    confidence: Confidence = 0.5
    explanation: Explanation = Field(default_factory=Explanation)
    error: str | None = None


class Principal(SioModel):
    """The authenticated actor behind a request. Threaded into every audit record."""

    subject: str = Field(description="User id or service account name")
    tenant_id: str
    roles: list[Role] = Field(default_factory=list)
    display_name: str | None = None
    email: str | None = None
    attributes: dict[str, Any] = Field(
        default_factory=dict, description="ABAC attributes: clearance, zones, shift, …"
    )
    token_id: str | None = None

    @property
    def is_admin(self) -> bool:
        return Role.ADMIN in self.roles

    def has_any(self, *roles: Role) -> bool:
        return any(r in self.roles for r in roles)


class AuditRecord(TenantScoped, Traced):
    """Append-only record of who did what, to which resource, and how it was decided.

    Written for *every* query, tool call, decision and approval. The table revokes UPDATE and
    DELETE, so this is the one part of SIO that cannot be quietly rewritten.
    """

    audit_id: str = Field(default_factory=lambda: new_id("aud"))
    ts: Timestamp = Field(default_factory=utc_now)
    actor: str = Field(description="Principal subject, or a service name for machine actions")
    actor_roles: list[Role] = Field(default_factory=list)
    action: str = Field(description="e.g. entities.list, copilot.ask, decision.approve")
    resource: str | None = None
    allowed: bool = True
    reason: str | None = Field(default=None, description="Policy rule that allowed or denied it")
    policy_engine: str | None = None
    ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def for_denial(
        cls, actor: str, action: str, resource: str, reason: str, **kw: Any
    ) -> AuditRecord:
        return cls(
            actor=actor, action=action, resource=resource, allowed=False, reason=reason, **kw
        )


class WebhookSubscription(TenantScoped):
    """Outbound notification target (PRD M22)."""

    webhook_id: str = Field(default_factory=lambda: new_id("whk"))
    url: str
    topics: list[str] = Field(
        default_factory=list, description="Bus topics or event types to forward"
    )
    secret: str | None = Field(default=None, description="HMAC-SHA256 signing secret")
    active: bool = True
    created_ts: Timestamp = Field(default_factory=utc_now)
    failure_count: int = Field(default=0, ge=0)
    last_delivery_ts: Timestamp | None = None
    last_error: str | None = None


class HealthStatus(SioModel):
    """Uniform ``/health`` payload for every service (PRD §13 observability)."""

    service: str
    status: str = Field(default="ok", description="ok | degraded | error")
    version: str = "0.1.0"
    schema_version: str
    uptime_s: float = 0.0
    checks: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "dependency → status. A value is healthy when it starts with 'ok'; anything else "
            "(error/unreachable/degraded) marks the service degraded. Informational values belong "
            "in `info`, not here."
        ),
    )
    info: dict[str, str] = Field(
        default_factory=dict,
        description="Non-status detail worth surfacing on /health (client counts, model names, …)",
    )
    consumed: int = 0
    produced: int = 0
    errors: int = 0
    lag: dict[str, int] = Field(default_factory=dict, description="topic → pending messages")
    adapters: dict[str, str] = Field(
        default_factory=dict, description="Which adapter is active per port, for debuggability"
    )


__all__ = [
    "AuditRecord",
    "EvidenceKind",
    "HealthStatus",
    "Mission",
    "MissionObjective",
    "Principal",
    "SimulationRun",
    "WebhookSubscription",
    "WorkflowRun",
    "WorkflowStep",
]
