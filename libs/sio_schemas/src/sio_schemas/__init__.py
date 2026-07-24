"""SIO shared data contracts.

Every payload that crosses a service boundary is defined here and nowhere else. Import from
the package root; the module layout is an implementation detail.
"""

from __future__ import annotations

from .base import (
    DEFAULT_TENANT,
    SCHEMA_VERSION,
    Confidence,
    SioModel,
    TenantScoped,
    Timestamp,
    Traced,
    new_id,
    utc_now,
)
from .bus import BusMessage, payload_model, register_payload
from .enums import (
    ActionType,
    AlertState,
    ApprovalState,
    EntityType,
    EventType,
    EvidenceKind,
    MissionState,
    Modality,
    RelationshipType,
    Role,
    RunStatus,
    Severity,
    SeverityRank,
    Topic,
    TrackStatus,
)
from .geo import BBox, Geo, Velocity
from .ops import (
    AuditRecord,
    HealthStatus,
    Mission,
    MissionObjective,
    Principal,
    SimulationRun,
    WebhookSubscription,
    WorkflowRun,
    WorkflowStep,
)
from .perception import Detection, Observation, Track, TrackState
from .reasoning import (
    Alert,
    Alternative,
    Decision,
    DecisionOption,
    EvidenceRef,
    Event,
    Explanation,
    Forecast,
    ForecastPoint,
    TimelineEntry,
)
from .world import Entity, EntityState, Provenance, Relationship

# Payloads that travel on the bus must be decodable from their envelope `kind`.
for _model in (
    Observation,
    Detection,
    Track,
    Entity,
    Relationship,
    Event,
    Forecast,
    Decision,
    Alert,
    Mission,
    WorkflowRun,
    SimulationRun,
    AuditRecord,
):
    register_payload(_model)

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_TENANT",
    "SCHEMA_VERSION",
    "ActionType",
    "Alert",
    "AlertState",
    "Alternative",
    "ApprovalState",
    "AuditRecord",
    "BBox",
    "BusMessage",
    "Confidence",
    "Decision",
    "DecisionOption",
    "Detection",
    "Entity",
    "EntityState",
    "EntityType",
    "Event",
    "EventType",
    "EvidenceKind",
    "EvidenceRef",
    "Explanation",
    "Forecast",
    "ForecastPoint",
    "Geo",
    "HealthStatus",
    "Mission",
    "MissionObjective",
    "MissionState",
    "Modality",
    "Observation",
    "Principal",
    "Provenance",
    "RelationshipType",
    "Relationship",
    "Role",
    "RunStatus",
    "Severity",
    "SeverityRank",
    "SimulationRun",
    "SioModel",
    "TenantScoped",
    "TimelineEntry",
    "Timestamp",
    "Topic",
    "Traced",
    "Track",
    "TrackState",
    "TrackStatus",
    "Velocity",
    "WebhookSubscription",
    "WorkflowRun",
    "WorkflowStep",
    "__version__",
    "new_id",
    "payload_model",
    "register_payload",
    "utc_now",
]
