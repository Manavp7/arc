"""Perception contracts: observation → detection → track (PRD §8.1)."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from .base import Confidence, SioModel, TenantScoped, Timestamp, Traced, new_id, utc_now
from .enums import Modality, TrackStatus
from .geo import BBox, Geo, Velocity


class Observation(TenantScoped, Traced):
    """One un-interpreted signal from one source at one instant.

    This is the universal envelope every connector produces (PRD M1). ``payload`` stays
    free-form because a weather reading and a MAVLink heartbeat have nothing in common;
    ``raw_ref`` points at the immutable bytes in the object store when they exist.
    """

    id: str = Field(default_factory=lambda: new_id("obs"))
    source_id: str = Field(min_length=1, description="Connector/sensor identifier, e.g. cam-gate-a")
    modality: Modality
    ts: Timestamp = Field(default_factory=utc_now)
    geo: Geo | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    confidence: Confidence = 1.0
    raw_ref: str | None = Field(
        default=None,
        description="Object-store key for the raw bytes (frame/audio/tile), if any",
    )
    schema_version: str | None = None


class Detection(TenantScoped, Traced):
    """A model's structured assertion about part of one observation (PRD M3).

    ``class_name`` is the PRD's ``class`` field; the alias keeps the wire name.
    """

    id: str = Field(default_factory=lambda: new_id("det"))
    observation_id: str
    class_name: str = Field(alias="class", min_length=1)
    bbox: BBox | None = None
    mask_ref: str | None = Field(default=None, description="Object-store key for an RLE mask")
    embedding_ref: str | None = Field(
        default=None, description="Key/row id for the ReID appearance vector, if computed"
    )
    confidence: Confidence
    ts: Timestamp = Field(default_factory=utc_now)
    source_id: str
    geo: Geo | None = Field(
        default=None, description="Ground position, once the spatial engine has projected it"
    )
    attrs: dict[str, Any] = Field(
        default_factory=dict,
        description="Model extras: OCR text, pose label, colour, audio class scores, …",
    )
    model_name: str | None = Field(default=None, description="Model that produced this detection")

    @property
    def text(self) -> str | None:
        """OCR text attached by the OCR engine, if any (plates, container ids, signage)."""
        value = self.attrs.get("text")
        return value if isinstance(value, str) else None


class TrackState(SioModel):
    """One time-step of a track: where the tracker believes the object was."""

    ts: Timestamp
    bbox: BBox | None = None
    geo: Geo | None = None
    velocity: Velocity | None = None
    confidence: Confidence = 1.0
    detection_id: str | None = Field(
        default=None, description="None when this state is a prediction through an occlusion"
    )


class Track(TenantScoped, Traced):
    """Persistent identity for one object as seen by one sensor over time (PRD M4)."""

    track_id: str = Field(default_factory=lambda: new_id("trk"))
    class_name: str = Field(alias="class")
    states: list[TrackState] = Field(default_factory=list)
    confidence: Confidence = 1.0
    source_id: str
    status: TrackStatus = TrackStatus.TENTATIVE
    start_ts: Timestamp = Field(default_factory=utc_now)
    last_ts: Timestamp = Field(default_factory=utc_now)
    hits: int = Field(default=0, ge=0, description="Detections associated to this track")
    age: int = Field(default=0, ge=0, description="Frames since the track was created")
    time_since_update: int = Field(default=0, ge=0)
    embedding: list[float] | None = Field(
        default=None, description="Smoothed ReID appearance vector (512-d) for re-association"
    )
    entity_id: str | None = Field(
        default=None, description="Set once fusion binds this track to a world-model entity"
    )
    cross_camera_of: list[str] = Field(
        default_factory=list, description="Track ids on other cameras believed to be the same object"
    )

    @property
    def latest(self) -> TrackState | None:
        return self.states[-1] if self.states else None

    @property
    def duration_s(self) -> float:
        return (self.last_ts - self.start_ts).total_seconds()

    def trajectory(self) -> list[Geo]:
        return [s.geo for s in self.states if s.geo is not None]
