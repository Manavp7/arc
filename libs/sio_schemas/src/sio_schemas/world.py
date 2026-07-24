"""World-model contracts: the fused entity and the bitemporal relationship (PRD M7)."""

from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from .base import Confidence, SioModel, TenantScoped, Timestamp, Traced, new_id, utc_now
from .enums import EntityType, Modality, RelationshipType
from .geo import Geo, Velocity


class Provenance(SioModel):
    """Which sensor contributed what, and how much we trusted it.

    Fusion appends one of these per contributing observation. This is the raw material for
    the "sources" section of every explanation, and the reason an operator can ask *why*
    SIO thinks a truck is at dock 3.
    """

    source_id: str
    modality: Modality
    ts: Timestamp
    observation_id: str | None = None
    detection_id: str | None = None
    track_id: str | None = None
    confidence: Confidence = 1.0
    weight: float = Field(default=1.0, ge=0.0, description="Sensor weight applied by fusion")
    note: str | None = None


class EntityState(SioModel):
    """The fused kinematic state of an entity at a point in time."""

    ts: Timestamp = Field(default_factory=utc_now)
    geo: Geo | None = None
    velocity: Velocity | None = None
    heading_deg: float | None = Field(default=None, ge=0.0, lt=360.0)
    zone_id: str | None = Field(default=None, description="Site zone containing the entity")
    h3_cell: str | None = Field(default=None, description="H3 index of the position")
    covariance: list[float] | None = Field(
        default=None,
        description="Flattened EKF position covariance (row-major), for uncertainty display",
    )
    confidence: Confidence = 1.0


class Entity(TenantScoped, Traced):
    """One unified real-world object — the heart of the world model.

    An entity is what N observations of the same thing collapse into (PRD M5/M7). Identity
    is stable across sensors and time, which is what makes "which camera last saw entity X"
    answerable.
    """

    entity_id: str = Field(default_factory=lambda: new_id("ent"))
    type: EntityType
    label: str | None = Field(default=None, description="Human-facing name, e.g. 'Truck ABC-123'")
    attributes: dict[str, Any] = Field(
        default_factory=dict,
        description="Type-specific facts: plate, colour, capacity, operator, criticality, …",
    )
    state: EntityState = Field(default_factory=EntityState)
    provenance: list[Provenance] = Field(default_factory=list)
    first_seen: Timestamp = Field(default_factory=utc_now)
    last_seen: Timestamp = Field(default_factory=utc_now)
    confidence: Confidence = 1.0
    track_ids: list[str] = Field(default_factory=list)
    is_static: bool = Field(
        default=False,
        description="Infrastructure (camera, gate, dock) rather than something that moves",
    )

    @property
    def sources(self) -> list[str]:
        """Distinct sensors that have contributed to this entity, newest first."""
        seen: dict[str, None] = {}
        for p in reversed(self.provenance):
            seen.setdefault(p.source_id, None)
        return list(seen)

    def dwell_s(self, now: Timestamp | None = None) -> float:
        """Seconds between first and last observation (UC1's "stayed more than 15 minutes")."""
        end = now or self.last_seen
        return (end - self.first_seen).total_seconds()


class Relationship(TenantScoped, Traced):
    """A bitemporal edge between two entities.

    Edges are *never* mutated: when a relationship stops holding we close it by setting
    ``ts_valid_to`` and, if it resumes, write a new edge. That is what lets the timeline
    reconstruct the graph as it stood at any instant (PRD M8, UC5) instead of only as it
    stands now.
    """

    id: str = Field(default_factory=lambda: new_id("rel"))
    from_id: str = Field(alias="from", min_length=1)
    type: RelationshipType
    to_id: str = Field(alias="to", min_length=1)
    ts_valid_from: Timestamp = Field(default_factory=utc_now)
    ts_valid_to: Timestamp | None = Field(
        default=None, description="None = still holds. Closing an edge never deletes it."
    )
    confidence: Confidence = 1.0
    attributes: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(
        default_factory=list,
        description="Ids of observations/detections/events supporting this edge",
    )

    @model_validator(mode="after")
    def _time_ordered(self) -> Relationship:
        if self.ts_valid_to is not None and self.ts_valid_to < self.ts_valid_from:
            raise ValueError("relationship ts_valid_to precedes ts_valid_from")
        return self

    @property
    def is_open(self) -> bool:
        return self.ts_valid_to is None

    def holds_at(self, ts: Timestamp) -> bool:
        """Was this edge valid at ``ts``? The primitive behind graph time-travel."""
        if ts < self.ts_valid_from:
            return False
        return self.ts_valid_to is None or ts <= self.ts_valid_to
