"""Normalising bus messages into flat facts.

The rule engine's whole premise is that adding a rule needs no code change (PRD M22). That is only
possible if rules are written against *one* shape, so every message the engine consumes — a fused
entity, a sensor reading, an upstream event, a detection — is flattened into a `Fact`: a dict of
dotted-path fields plus a timestamp and a few keys the engine groups by.

Flattening is deliberately shallow and explicit rather than a generic recursive walk of the payload.
A generic walk would expose every internal field to rule authors, which sounds flexible and is a trap:
the rules become coupled to the wire format, and renaming a field inside a service breaks a YAML file
nobody remembers writing. What is exposed here is a *contract*, and it is small enough to document.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sio_schemas import Detection, Entity, Event, Observation, Track, utc_now


@dataclass
class Fact:
    """One normalised thing that happened, ready for rule evaluation."""

    kind: str
    """``entity`` | ``observation`` | ``event`` | ``detection`` | ``track``."""
    ts: datetime
    fields: dict[str, Any] = field(default_factory=dict)
    entity_id: str | None = None
    zone_id: str | None = None
    source_id: str | None = None
    tenant_id: str = "default"
    evidence_ref: str | None = None
    """The id a fired rule should cite as evidence."""
    raw: Any = None
    """The originating model, so a rule that fires can attach real provenance."""

    def get(self, path: str) -> Any:
        """Look up a dotted path. Missing paths return None rather than raising.

        Missing-is-None matters: a rule asking about ``speed_mps`` should simply not match a fact that
        has no speed, instead of taking down the engine for every message from a thermometer.
        """
        if path in self.fields:
            return self.fields[path]
        current: Any = self.fields
        for part in path.split("."):
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None
            if current is None:
                return None
        return current

    def keyed_by(self, keys: tuple[str, ...]) -> str:
        """A grouping key, used for per-subject windows and cooldowns."""
        return "|".join(str(self.get(key) or "") for key in keys) or "*"


def fact_from_entity(entity: Entity) -> Fact:
    """A fused entity's current state.

    Speed is exposed in both m/s and km/h because rules are written by humans: a yard speed limit is
    posted in km/h, and forcing every author to divide by 3.6 in YAML is how a rule ends up wrong by a
    factor of 3.6.
    """
    state = entity.state
    speed_mps = 0.0
    if state is not None and state.velocity is not None:
        speed_mps = float(math.hypot(state.velocity.east or 0.0, state.velocity.north or 0.0))
    return Fact(
        kind="entity",
        ts=state.ts if state is not None else entity.last_seen,
        entity_id=entity.entity_id,
        zone_id=state.zone_id if state is not None else None,
        tenant_id=entity.tenant_id,
        evidence_ref=entity.entity_id,
        raw=entity,
        fields={
            "entity_id": entity.entity_id,
            "entity_type": str(entity.type),
            "label": entity.label,
            "confidence": entity.confidence,
            "is_static": entity.is_static,
            "speed_mps": round(speed_mps, 3),
            "speed_kmh": round(speed_mps * 3.6, 2),
            "heading_deg": state.heading_deg if state is not None else None,
            "lat": state.geo.lat if state is not None and state.geo else None,
            "lon": state.geo.lon if state is not None and state.geo else None,
            "zone_id": state.zone_id if state is not None else None,
            "modalities": entity.attributes.get("modalities", []),
            "observations": entity.attributes.get("observations", 0),
            "position_sigma_m": entity.attributes.get("position_sigma_m"),
            "track_count": len(entity.track_ids),
            "dwell_s": entity.attributes.get("dwell_s", 0.0),
            "attributes": dict(entity.attributes),
        },
    )


def fact_from_observation(observation: Observation) -> Fact:
    """A sensor reading. The payload is exposed under ``payload.*`` so rules can reach metric values."""
    payload = dict(observation.payload)
    return Fact(
        kind="observation",
        ts=observation.ts,
        source_id=observation.source_id,
        zone_id=payload.get("zone_id"),
        tenant_id=observation.tenant_id,
        evidence_ref=observation.id,
        raw=observation,
        fields={
            "source_id": observation.source_id,
            "modality": str(observation.modality),
            "metric": payload.get("metric"),
            "value": payload.get("value"),
            "unit": payload.get("unit"),
            "zone_id": payload.get("zone_id"),
            "lat": observation.geo.lat if observation.geo else None,
            "lon": observation.geo.lon if observation.geo else None,
            "payload": payload,
        },
    )


def fact_from_event(event: Event) -> Fact:
    """An event from another producer.

    Rules over events are what make composition possible: `zone_breach` is "an unauthorised entry into
    a restricted zone *and* the entity is a vehicle", and expressing that by re-deriving geometry inside
    the events service would duplicate the spatial service's one implementation.
    """
    return Fact(
        kind="event",
        ts=event.ts,
        entity_id=event.entities[0] if event.entities else None,
        zone_id=event.zone_id,
        source_id=event.source_ids[0] if event.source_ids else None,
        tenant_id=event.tenant_id,
        evidence_ref=event.event_id,
        raw=event,
        fields={
            "event_id": event.event_id,
            "event_type": str(event.type),
            "severity": str(event.severity),
            "entity_id": event.entities[0] if event.entities else None,
            "entities": list(event.entities),
            "zone_id": event.zone_id,
            "rule_id": event.rule_id,
            "confidence": event.confidence,
            "lat": event.geo.lat if event.geo else None,
            "lon": event.geo.lon if event.geo else None,
            "attributes": dict(event.attributes),
            "dwell_s": event.attributes.get("dwell_s", 0.0),
            "restricted": event.attributes.get("restricted", False),
            "entity_type": event.attributes.get("entity_type"),
        },
    )


def fact_from_detection(detection: Detection) -> Fact:
    """A raw detection. Used by rules that must not wait for tracking or fusion — fire, above all.

    Fire is the one thing where the latency of the full pipeline is unacceptable: a detector that sees
    flame should raise it now, with lower confidence, rather than three seconds later with a track id.
    """
    bbox = detection.bbox
    return Fact(
        kind="detection",
        ts=detection.ts,
        source_id=detection.source_id,
        tenant_id=detection.tenant_id,
        evidence_ref=detection.id,
        raw=detection,
        fields={
            "detection_id": detection.id,
            "observation_id": detection.observation_id,
            "class": detection.class_name,
            "class_name": detection.class_name,
            "confidence": detection.confidence,
            "source_id": detection.source_id,
            "area_px": bbox.area if bbox else None,
            # Aspect ratio is exposed because a rule needs it: person_fell keys on a person's box
            # becoming wider than tall. Deriving it in YAML is not possible, and adding a bespoke
            # "fall detector" for one ratio would be code where data suffices.
            "aspect_ratio": round(bbox.width / bbox.height, 3) if bbox and bbox.height else None,
            "attributes": dict(detection.attrs),
        },
    )


def fact_from_track(track: Track) -> Fact:
    latest = track.latest
    return Fact(
        kind="track",
        ts=track.last_ts,
        source_id=track.source_id,
        tenant_id=track.tenant_id,
        evidence_ref=track.track_id,
        raw=track,
        fields={
            "track_id": track.track_id,
            "class": track.class_name,
            "class_name": track.class_name,
            "confidence": track.confidence,
            "source_id": track.source_id,
            "status": str(track.status),
            "hits": track.hits,
            "age": track.age,
            "speed_px_s": (
                math.hypot(latest.velocity.east or 0.0, latest.velocity.north or 0.0)
                if latest is not None and latest.velocity is not None
                else None
            ),
        },
    )


def fact_from_message(kind: str, decode: Any) -> Fact | None:
    """Normalise a bus message by its declared kind.

    Unknown kinds return None rather than raising: the engine subscribes to broad topics, and a new
    message type appearing on one of them is not an error — it is just not something any rule can
    match yet.
    """
    if kind == "Entity":
        return fact_from_entity(decode(Entity))
    if kind == "Observation":
        return fact_from_observation(decode(Observation))
    if kind == "Event":
        return fact_from_event(decode(Event))
    if kind == "Detection":
        return fact_from_detection(decode(Detection))
    if kind == "Track":
        return fact_from_track(decode(Track))
    return None


def synthetic_fact(kind: str, fields: dict[str, Any], **kwargs: Any) -> Fact:
    """Build a fact directly. Used by the absence detector, which fires on what did *not* arrive."""
    return Fact(kind=kind, ts=kwargs.pop("ts", utc_now()), fields=fields, **kwargs)
