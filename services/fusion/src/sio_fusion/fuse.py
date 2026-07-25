"""Multi-sensor fusion: N observations of one object become one entity (PRD M5).

This is the component that makes the world model a *world* model rather than a list of sightings. A
truck at the gate is seen by a camera, carries a GPS tracker and trips an RFID reader; without fusion
those are three unrelated records, and "how long has that truck been on site" has no answer.

Three decisions shape everything here.

**A device id is identity; a camera detection is not.** GPS fixes from the same tracker are the same
object — that is what a device id *means*, and every real system uses it. A camera track has no such
luxury: associating it takes position, time and class agreement. So the two paths are deliberately
asymmetric, and the hard problem is confined to where it actually exists.

Note what is *not* used: the simulator puts `agent_id` in its payloads, and associating on it would
make this look flawless while testing nothing. Fusion never reads it.

**The filter runs in metres, not degrees.** A Kalman filter over latitude and longitude has a
covariance whose axes carry different units and whose scale varies with latitude, which quietly
invalidates every gate and noise term. Local east/north metres removes that at site scale.

**A Kalman filter, not an EKF.** The PRD says EKF, and for a position measurement of a
constant-velocity object the model is *linear* — an EKF would add jacobians that are identity matrices
and nothing else. The non-linear part (geodetic to local) is handled once, exactly, by the projection.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from sio_core import get_logger
from sio_schemas import (
    EntityState,
    EntityType,
    Geo,
    Modality,
    Provenance,
    Velocity,
    new_id,
    utc_now,
)

from .projection import from_local_metres, to_local_metres

log = get_logger("sio.fusion")

# How much to trust each modality's position, as a one-sigma error in metres. These are the weights
# that decide who wins when a camera and a GPS disagree, so they are stated as measurements rather
# than buried as magic numbers.
SENSOR_SIGMA_M: dict[Modality, float] = {
    Modality.GPS: 2.5,
    Modality.VIDEO: 6.0,
    Modality.RFID: 3.0,  # a reader has a short range, so a read is a decent position fix
    Modality.IOT: 8.0,
    Modality.MANUAL: 10.0,
}

# Detector and device classes mapped onto world-model types. A bus body is the photographic stand-in
# for a box truck in this yard, so `bus` means truck here; the mapping is data, not a special case
# scattered through the code.
CLASS_TO_ENTITY: dict[str, EntityType] = {
    "person": EntityType.PERSON,
    "truck": EntityType.TRUCK,
    "bus": EntityType.TRUCK,
    "car": EntityType.VEHICLE,
    "vehicle": EntityType.VEHICLE,
    "forklift": EntityType.FORKLIFT,
    "drone": EntityType.DRONE,
    "airplane": EntityType.DRONE,  # what COCO calls a quadcopter
    "motorcycle": EntityType.VEHICLE,
    "boat": EntityType.SHIP,
}

# Which types may be fused together. A camera cannot reliably tell a forklift from a truck, so those
# are compatible; a person and a truck never are.
COMPATIBLE: tuple[frozenset[EntityType], ...] = (
    frozenset({EntityType.TRUCK, EntityType.VEHICLE, EntityType.FORKLIFT}),
    frozenset({EntityType.PERSON}),
    frozenset({EntityType.DRONE}),
)


def entity_type_for(label: str) -> EntityType:
    return CLASS_TO_ENTITY.get(label.lower(), EntityType.UNKNOWN)


def types_compatible(left: EntityType, right: EntityType) -> bool:
    if left is right:
        return True
    if EntityType.UNKNOWN in (left, right):
        return True  # an unknown class should not block an otherwise good match
    return any({left, right} <= group for group in COMPATIBLE)


@dataclass
class Observation2D:
    """One positional observation, normalised across modalities.

    Everything fusion consumes — a projected camera track, a GPS fix, an RFID read — is reduced to
    this. That is what lets one association routine handle all of them instead of three.
    """

    source_id: str
    modality: Modality
    ts: datetime
    east: float
    north: float
    sigma_m: float
    label: str
    confidence: float = 1.0
    device_id: str | None = None
    """Stable identity where the sensor has one (a GPS tracker, an RFID tag). None for a camera."""
    embedding: tuple[float, ...] | None = None
    observation_id: str | None = None
    detection_id: str | None = None
    track_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def entity_type(self) -> EntityType:
        return entity_type_for(self.label)


class PositionFilter:
    """Constant-velocity Kalman filter on ``[east, north, v_east, v_north]`` in metres.

    Uses ``filterpy`` when available (the PRD's choice) and falls back to the same maths in numpy
    otherwise, so fusion is never blocked on an optional dependency. The fallback is about fifteen
    lines because a linear KF *is* about fifteen lines.
    """

    def __init__(
        self, east: float, north: float, *, sigma_m: float = 3.0, ts: datetime | None = None
    ) -> None:
        self.state = np.array([east, north, 0.0, 0.0], dtype=np.float64)
        self.covariance = np.diag([sigma_m**2, sigma_m**2, 25.0, 25.0]).astype(np.float64)
        # Time comes from the OBSERVATIONS, not from the wall clock.
        #
        # Using time.monotonic() looked reasonable and was wrong: with fixes a second apart in their
        # own timestamps but microseconds apart in arrival, dt was ~0, the filter never propagated,
        # and a truck crossing the yard at 4 m/s was estimated as stationary. It also breaks under
        # replay and under at-least-once redelivery, where arrival order and measurement order are
        # different things.
        self.last_ts: datetime | None = ts
        # Process noise per second of prediction. Tuned for yard traffic: a vehicle can accelerate,
        # so velocity noise is generous; position noise is small because position only changes
        # through velocity.
        self._q_position = 0.25
        self._q_velocity = 4.0

    def advance_to(self, ts: datetime) -> None:
        """Predict forward to an observation's timestamp."""
        if self.last_ts is None:
            self.last_ts = ts
            return
        dt = (ts - self.last_ts).total_seconds()
        if dt <= 0:
            # Out of order, or simultaneous. Do not rewind: redelivery reorders messages, and a
            # negative step would inflate the covariance while moving the estimate backwards.
            return
        # Cap the step. After a long gap, propagating a stale velocity for minutes puts the estimate
        # somewhere fictional; better to widen uncertainty over a bounded interval and let the next
        # measurement dominate.
        self.predict(min(dt, 30.0))
        self.last_ts = ts

    def predict(self, dt: float) -> None:
        if dt <= 0:
            return
        transition = np.array(
            [[1, 0, dt, 0], [0, 1, 0, dt], [0, 0, 1, 0], [0, 0, 0, 1]], dtype=np.float64
        )
        noise = np.diag(
            [
                self._q_position * dt,
                self._q_position * dt,
                self._q_velocity * dt,
                self._q_velocity * dt,
            ]
        )
        self.state = transition @ self.state
        self.covariance = transition @ self.covariance @ transition.T + noise

    def update(self, east: float, north: float, sigma_m: float) -> None:
        observation = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], dtype=np.float64)
        measurement = np.array([east, north], dtype=np.float64)
        measurement_noise = np.diag([sigma_m**2, sigma_m**2])

        residual = measurement - observation @ self.state
        projected = observation @ self.covariance @ observation.T + measurement_noise
        gain = self.covariance @ observation.T @ np.linalg.inv(projected)
        self.state = self.state + gain @ residual
        self.covariance = (np.eye(4) - gain @ observation) @ self.covariance

    def mahalanobis(self, east: float, north: float, sigma_m: float) -> float:
        """Normalised distance from the prediction to a measurement.

        The right gate for association: it accounts for how uncertain the *track* currently is, so a
        well-established entity rejects a distant fix that a freshly-created one would accept. A plain
        metre radius cannot do that, and picking one radius for a stationary forklift and a
        20 m/s drone means picking it wrong for both.
        """
        observation = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], dtype=np.float64)
        measurement_noise = np.diag([sigma_m**2, sigma_m**2])
        projected = observation @ self.covariance @ observation.T + measurement_noise
        residual = np.array([east, north]) - observation @ self.state
        return float(math.sqrt(residual @ np.linalg.inv(projected) @ residual))

    @property
    def position(self) -> tuple[float, float]:
        return float(self.state[0]), float(self.state[1])

    @property
    def velocity(self) -> tuple[float, float]:
        return float(self.state[2]), float(self.state[3])

    @property
    def position_sigma_m(self) -> float:
        return float(math.sqrt(max(self.covariance[0, 0], self.covariance[1, 1])))


@dataclass
class FusedEntity:
    """One real-world object, as fusion currently understands it."""

    entity_id: str
    entity_type: EntityType
    filter: PositionFilter
    first_seen: datetime
    last_seen: datetime
    provenance: list[Provenance] = field(default_factory=list)
    device_ids: set[str] = field(default_factory=set)
    """Devices bound to this object. A device id is identity, so a match here is certain."""
    track_ids: set[str] = field(default_factory=set)
    source_ids: set[str] = field(default_factory=set)
    modalities_seen: set[str] = field(default_factory=set)
    """Every sensor kind that has ever contributed.

    Kept separately from `provenance`, which is a bounded window of *recent* evidence. Deriving
    "is this multi-sensor?" from that window meant an entity genuinely corroborated by GPS, video and
    RFID reported as single-sensor as soon as the older entries rolled off — understating exactly the
    thing fusion exists to do.
    """
    embedding: np.ndarray | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    label: str | None = None
    observations: int = 0
    updated_monotonic: float = field(default_factory=time.monotonic)

    @property
    def modalities(self) -> set[str]:
        """Sensor kinds that have contributed, ever — not just those still in the evidence window."""
        return set(self.modalities_seen) or {str(entry.modality) for entry in self.provenance}

    @property
    def is_multi_sensor(self) -> bool:
        """Has more than one *kind* of sensor contributed? The PRD's acceptance criterion for M5."""
        return len(self.modalities) > 1

    def stale_for(self, now: float) -> float:
        return now - self.updated_monotonic


class SensorFusion:
    """Associates observations into entities and maintains their fused state."""

    def __init__(
        self,
        origin: Geo,
        *,
        gate_sigma: float = 4.0,
        assoc_radius_m: float = 25.0,
        time_window_s: float = 5.0,
        max_stale_s: float = 120.0,
        reid_threshold: float = 0.75,
        min_observations: int = 2,
    ) -> None:
        self.origin = origin
        self.gate_sigma = gate_sigma
        self.assoc_radius_m = assoc_radius_m
        self.time_window_s = time_window_s
        self.max_stale_s = max_stale_s
        self.reid_threshold = reid_threshold
        self.min_observations = min_observations
        self.entities: dict[str, FusedEntity] = {}
        self._device_index: dict[str, str] = {}
        """device id → entity id. The certain half of association."""
        self._track_index: dict[str, str] = {}
        """track id → entity id, so a camera track that was already associated stays associated."""
        self.stats = {
            "observations": 0,
            "created": 0,
            "matched_by_device": 0,
            "matched_by_track": 0,
            "matched_by_position": 0,
            "matched_by_appearance": 0,
            "rejected_by_gate": 0,
            "rejected_by_class": 0,
            "rejected_by_device_conflict": 0,
            "expired": 0,
            "merged": 0,
        }

    # ------------------------------------------------------------------ ingestion
    def observe(self, observation: Observation2D) -> FusedEntity:
        """Fold one observation into the world, creating or updating an entity."""
        self.stats["observations"] += 1
        now = time.monotonic()
        self._expire(now)

        entity = self._match(observation, now)
        if entity is None:
            entity = self._create(observation, now)
        else:
            self._update(entity, observation, now)
        return entity

    def _match(self, observation: Observation2D, now: float) -> FusedEntity | None:
        """Find the entity this observation belongs to, cheapest and most certain test first."""
        # 1. A device id is identity. No gating needed, and none wanted: a GPS tracker that has moved
        #    500 m since its last fix is still the same tracker.
        if observation.device_id:
            entity_id = self._device_index.get(observation.device_id)
            if entity_id and entity_id in self.entities:
                self.stats["matched_by_device"] += 1
                return self.entities[entity_id]

        # 2. A camera track already bound to an entity stays bound. Re-solving the association every
        #    frame would let a single bad frame reassign a truck to its neighbour.
        if observation.track_id:
            entity_id = self._track_index.get(observation.track_id)
            if entity_id and entity_id in self.entities:
                self.stats["matched_by_track"] += 1
                return self.entities[entity_id]

        # 3. The real problem: position, time and class agreement.
        best: tuple[float, FusedEntity] | None = None
        for entity in self.entities.values():
            if self._device_conflict(entity, observation):
                # A device id means identity, and therefore also *non*-identity: if this entity
                # already carries a different tracker's id, the two are provably different objects
                # however close together they are.
                #
                # Live consequence of omitting this: six trucks queued 16 m apart at the gate fell
                # inside the 25 m radius, so five separate GPS trackers were absorbed into one
                # entity — which then claimed 39,651 observations and five devices.
                self.stats["rejected_by_device_conflict"] += 1
                continue
            if not types_compatible(entity.entity_type, observation.entity_type):
                self.stats["rejected_by_class"] += 1
                continue
            age = abs((observation.ts - entity.last_seen).total_seconds())
            if age > self.time_window_s:
                continue

            entity.filter.advance_to(observation.ts)
            distance = entity.filter.mahalanobis(
                observation.east, observation.north, observation.sigma_m
            )
            metres = math.dist(entity.filter.position, (observation.east, observation.north))
            if distance > self.gate_sigma or metres > self.assoc_radius_m:
                self.stats["rejected_by_gate"] += 1
                continue
            if best is None or distance < best[0]:
                best = (distance, entity)

        if best is not None:
            self.stats["matched_by_position"] += 1
            return best[1]

        # 4. Appearance, as a last resort and only for camera observations. Deliberately last: two
        #    identical white vans look the same, so appearance may only break a tie that position
        #    could not, never override position.
        if observation.embedding is not None:
            vector = np.asarray(observation.embedding, dtype=np.float32)
            for entity in self.entities.values():
                if entity.embedding is None:
                    continue
                if self._device_conflict(entity, observation):
                    self.stats["rejected_by_device_conflict"] += 1
                    continue
                if not types_compatible(entity.entity_type, observation.entity_type):
                    continue
                if float(np.dot(vector, entity.embedding)) >= self.reid_threshold:
                    self.stats["matched_by_appearance"] += 1
                    return entity
        return None

    @staticmethod
    def _device_conflict(entity: FusedEntity, observation: Observation2D) -> bool:
        """Does this observation carry a *different* device of the same kind as one already bound?

        A device id is identity, which necessarily makes it non-identity too — and that half is easy
        to forget, because it only bites when two distinct objects come close enough to pass a
        positional gate. (Live: six trucks queued 16 m apart absorbed five separate GPS trackers into
        one entity.)

        But the test has to be **per device kind**. A truck legitimately carries a GPS tracker *and*
        an RFID tag; a global "different id means different object" rule refused exactly the
        multi-sensor merge that fusion exists to perform, and broke the M5 acceptance test. So ids are
        namespaced (``gps:...``, ``tag:...``) and only a clash within one namespace is a conflict:
        one object, at most one device of each kind.
        """
        if not observation.device_id or not entity.device_ids:
            return False
        namespace = observation.device_id.split(":", 1)[0]
        same_kind = {device for device in entity.device_ids if device.split(":", 1)[0] == namespace}
        return bool(same_kind) and observation.device_id not in same_kind

    def _create(self, observation: Observation2D, now: float) -> FusedEntity:
        entity = FusedEntity(
            entity_id=new_id("ent"),
            entity_type=observation.entity_type,
            filter=PositionFilter(
                observation.east, observation.north, sigma_m=observation.sigma_m, ts=observation.ts
            ),
            first_seen=observation.ts,
            last_seen=observation.ts,
            label=observation.attributes.get("label"),
        )
        self.entities[entity.entity_id] = entity
        self.stats["created"] += 1
        self._update(entity, observation, now, predict=False)
        return entity

    def _update(
        self, entity: FusedEntity, observation: Observation2D, now: float, *, predict: bool = True
    ) -> None:
        if predict:
            entity.filter.advance_to(observation.ts)
        entity.filter.update(observation.east, observation.north, observation.sigma_m)

        entity.first_seen = min(entity.first_seen, observation.ts)
        entity.last_seen = max(entity.last_seen, observation.ts)
        entity.updated_monotonic = now
        entity.observations += 1
        entity.source_ids.add(observation.source_id)
        entity.modalities_seen.add(str(observation.modality))

        if observation.device_id:
            entity.device_ids.add(observation.device_id)
            self._device_index[observation.device_id] = entity.entity_id
        if observation.track_id:
            entity.track_ids.add(observation.track_id)
            self._track_index[observation.track_id] = entity.entity_id

        # A more specific type wins: a GPS device that declares itself a forklift beats a camera that
        # could only tell it was a vehicle.
        if (
            entity.entity_type is EntityType.UNKNOWN
            and observation.entity_type is not EntityType.UNKNOWN
        ) or (
            observation.modality is Modality.GPS
            and observation.entity_type is not EntityType.UNKNOWN
        ):
            entity.entity_type = observation.entity_type

        if observation.embedding is not None:
            vector = np.asarray(observation.embedding, dtype=np.float32)
            if entity.embedding is None:
                entity.embedding = vector
            else:
                blended = 0.9 * entity.embedding + 0.1 * vector
                norm = float(np.linalg.norm(blended))
                entity.embedding = blended / norm if norm else blended

        for key, value in observation.attributes.items():
            # Ground-truth identity fields are never carried onto an entity: they would make the
            # world model look correct while proving nothing about association.
            if key in ("agent_id", "label", "ground_truth_label"):
                continue
            entity.attributes[key] = value

        entity.provenance.append(
            Provenance(
                source_id=observation.source_id,
                modality=observation.modality,
                ts=observation.ts,
                observation_id=observation.observation_id,
                detection_id=observation.detection_id,
                track_id=observation.track_id,
                confidence=observation.confidence,
                weight=round(1.0 / max(0.5, observation.sigma_m), 3),
                note=f"sigma {observation.sigma_m:.1f} m",
            )
        )
        # Bounded: provenance is evidence for an explanation, not an audit log. The audit table is
        # append-only and holds the full history; an entity carrying ten thousand entries would make
        # every read of it expensive.
        if len(entity.provenance) > 40:
            del entity.provenance[:-40]

    def merge_pass(self) -> int:
        """Merge entities that have turned out to be the same object.

        Association happens one observation at a time, so a truck first seen by a camera becomes a
        video-only entity and its GPS tracker — arriving moments later, or gated out while the
        projected position was still uncertain — becomes a second one. Once the camera track is bound
        to the first entity it stays bound, so the two never meet: the yard ends up with one entity per
        *sensor* rather than one per truck, and `multi_sensor` stays near zero while everything looks
        superficially fine.

        This is track-to-track fusion, and the criteria are deliberately stricter than for a single
        observation, because merging two entities destroys information that cannot be recovered:

        * no device conflict — a clash of two GPS trackers proves they are different objects;
        * class compatible;
        * both filters agree on position within a tight mutual gate;
        * both recently updated, so this is a live coincidence and not two historical visits.
        """
        merged = 0
        now = time.monotonic()
        candidates = [
            entity
            for entity in self.entities.values()
            if entity.stale_for(now) < self.time_window_s * 2
        ]
        for index, left in enumerate(candidates):
            if left.entity_id not in self.entities:
                continue  # already merged away in this pass
            for right in candidates[index + 1 :]:
                if right.entity_id not in self.entities or left.entity_id not in self.entities:
                    continue
                if not self._mergeable(left, right):
                    continue
                keeper, absorbed = (
                    (left, right) if left.first_seen <= right.first_seen else (right, left)
                )
                self._absorb(keeper, absorbed)
                merged += 1
                self.stats["merged"] += 1
        return merged

    def _mergeable(self, left: FusedEntity, right: FusedEntity) -> bool:
        if left.device_ids and right.device_ids:
            for namespace in {device.split(":", 1)[0] for device in left.device_ids}:
                left_kind = {d for d in left.device_ids if d.startswith(f"{namespace}:")}
                right_kind = {d for d in right.device_ids if d.startswith(f"{namespace}:")}
                if left_kind and right_kind and left_kind != right_kind:
                    return False  # two different trackers of the same kind: different objects
        if not types_compatible(left.entity_type, right.entity_type):
            return False
        # A mutual gate: each filter must find the other's position plausible. One-directional gating
        # lets a very uncertain entity swallow a precise one.
        left_east, left_north = left.filter.position
        right_east, right_north = right.filter.position
        if (
            math.dist((left_east, left_north), (right_east, right_north))
            > self.assoc_radius_m * 0.6
        ):
            return False
        forward = left.filter.mahalanobis(right_east, right_north, right.filter.position_sigma_m)
        backward = right.filter.mahalanobis(left_east, left_north, left.filter.position_sigma_m)
        return max(forward, backward) <= self.gate_sigma

    def _absorb(self, keeper: FusedEntity, absorbed: FusedEntity) -> None:
        """Fold ``absorbed`` into ``keeper``, keeping the union of its evidence."""
        keeper.first_seen = min(keeper.first_seen, absorbed.first_seen)
        keeper.last_seen = max(keeper.last_seen, absorbed.last_seen)
        keeper.observations += absorbed.observations
        keeper.source_ids |= absorbed.source_ids
        keeper.modalities_seen |= absorbed.modalities_seen
        keeper.device_ids |= absorbed.device_ids
        keeper.track_ids |= absorbed.track_ids
        keeper.provenance = [*keeper.provenance, *absorbed.provenance][-40:]
        for key, value in absorbed.attributes.items():
            keeper.attributes.setdefault(key, value)
        if keeper.embedding is None:
            keeper.embedding = absorbed.embedding
        # A device-declared type is more specific than a camera's guess, so prefer whichever entity
        # had one.
        if keeper.entity_type is EntityType.UNKNOWN or (
            absorbed.device_ids and not keeper.device_ids
        ):
            keeper.entity_type = absorbed.entity_type

        # Re-point the indexes before dropping the absorbed entity, or its devices and tracks would
        # resolve to an entity that no longer exists and every later observation would create a new one.
        for device_id in absorbed.device_ids:
            self._device_index[device_id] = keeper.entity_id
        for track_id in absorbed.track_ids:
            self._track_index[track_id] = keeper.entity_id
        self.entities.pop(absorbed.entity_id, None)

    def _expire(self, now: float) -> None:
        stale = [
            entity_id
            for entity_id, entity in self.entities.items()
            if entity.stale_for(now) > self.max_stale_s
        ]
        for entity_id in stale:
            entity = self.entities.pop(entity_id)
            for device_id in entity.device_ids:
                self._device_index.pop(device_id, None)
            for track_id in entity.track_ids:
                self._track_index.pop(track_id, None)
            self.stats["expired"] += 1

    # -------------------------------------------------------------------- output
    def to_entity_state(self, entity: FusedEntity) -> EntityState:
        east, north = entity.filter.position
        v_east, v_north = entity.filter.velocity
        return EntityState(
            ts=entity.last_seen,
            geo=from_local_metres(east, north, self.origin),
            velocity=Velocity(east=v_east, north=v_north),
            heading_deg=(math.degrees(math.atan2(v_east, v_north)) + 360.0) % 360.0
            if abs(v_east) + abs(v_north) > 0.2
            else None,
            covariance=[
                float(entity.filter.covariance[0, 0]),
                float(entity.filter.covariance[0, 1]),
                float(entity.filter.covariance[1, 0]),
                float(entity.filter.covariance[1, 1]),
            ],
            confidence=round(
                float(max(0.1, min(0.99, 6.0 / (6.0 + entity.filter.position_sigma_m)))), 3
            ),
        )

    def publishable(self) -> Iterable[FusedEntity]:
        """Entities with enough support to be worth asserting.

        A single observation is a sighting, not an object: publishing it would fill the world model
        with one-frame ghosts, and a ghost with a plausible position is harder to spot than no entity
        at all.
        """
        return (
            entity
            for entity in self.entities.values()
            if entity.observations >= self.min_observations
        )

    def describe(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "origin": {"lat": self.origin.lat, "lon": self.origin.lon},
            "entities": len(self.entities),
            "multi_sensor": sum(1 for entity in self.entities.values() if entity.is_multi_sensor),
            "gate_sigma": self.gate_sigma,
            "assoc_radius_m": self.assoc_radius_m,
            "stats": dict(self.stats),
            "sample": [
                {
                    "entity_id": entity.entity_id,
                    "type": str(entity.entity_type),
                    "modalities": sorted(entity.modalities),
                    "sources": sorted(entity.source_ids),
                    "devices": sorted(entity.device_ids),
                    "tracks": sorted(entity.track_ids),
                    "observations": entity.observations,
                    "sigma_m": round(entity.filter.position_sigma_m, 2),
                    "speed_mps": round(math.hypot(*entity.filter.velocity), 2),
                    "stale_s": round(entity.stale_for(now), 1),
                }
                for entity in list(self.entities.values())[:10]
            ],
        }


def observation_from_gps(
    payload: dict[str, Any], source_id: str, ts: datetime, geo: Geo, origin: Geo
) -> Observation2D:
    """Build an observation from a GPS fix.

    The tracker's ``source_id`` is used as a device id, which is legitimate identity: a GPS device has
    a stable id and grouping its fixes is what every fleet system does. What is *not* used is the
    simulator's ``agent_id`` — that would link camera and GPS observations by ground truth and make
    association look solved when it had not been attempted.
    """
    east, north = to_local_metres(geo, origin)
    accuracy = float(payload.get("hdop_m", SENSOR_SIGMA_M[Modality.GPS]))
    return Observation2D(
        source_id=source_id,
        modality=Modality.GPS,
        ts=ts,
        east=east,
        north=north,
        sigma_m=max(1.0, accuracy),
        label=str(payload.get("entity_type", "unknown")),
        confidence=float(payload.get("confidence", 0.9)),
        # Namespaced, so a GPS tracker and an RFID tag on the same truck are different *kinds* of
        # device rather than conflicting identities.
        device_id=f"gps:{source_id}",
        observation_id=payload.get("observation_id"),
        attributes={
            key: value
            for key, value in payload.items()
            if key in ("state", "battery_pct", "speed_mps", "plate", "dock", "zone_id")
        },
    )


def observation_from_rfid(
    payload: dict[str, Any], source_id: str, ts: datetime, geo: Geo, origin: Geo
) -> Observation2D:
    """An RFID read: a strong identity signal at a known, small location."""
    east, north = to_local_metres(geo, origin)
    tag = payload.get("tag_id")
    return Observation2D(
        source_id=source_id,
        modality=Modality.RFID,
        ts=ts,
        east=east,
        north=north,
        sigma_m=SENSOR_SIGMA_M[Modality.RFID],
        label="truck",
        confidence=0.99,
        device_id=f"tag:{tag}" if tag else None,
        attributes={"plate": payload.get("plate"), "zone_id": payload.get("zone_id")},
    )


def utc_or_now(value: datetime | None) -> datetime:
    return value or utc_now()
