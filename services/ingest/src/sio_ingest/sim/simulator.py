"""The yard simulator: turns agent motion into bus traffic.

One tick produces three kinds of output:

* **observations** — what sensors would actually report: GPS fixes, camera frames (with the agents
  inside each field of view), IoT readings, RFID reads. This is the real ingestion path.
* **ground-truth entities** — a Phase 1 bridge. Until `perception → tracking → fusion` exists
  (Phase 2), something has to put entities in the world model for the map to show. It is published
  under `attributes.simulated = true` with explicit provenance, and switched off with
  `SIO_SIM_PUBLISH_ENTITIES=false` once fusion is live. Being a *bridge* rather than the design is
  why it is one flag and one clearly-labelled method.
* **site entities** — the fixed cast (cameras, sensors, gates, docks), emitted once so the map has
  context and so "which camera covers Gate B" has something to answer with.

Injectable incidents (`inject_fire`, `inject_power_failure`) are how the demo and the e2e tests
produce a scenario on demand instead of waiting for one.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sio_schemas import (
    EntityState,
    EntityType,
    Geo,
    Modality,
    Observation,
    Provenance,
    Topic,
    new_id,
    utc_now,
)

from ..site import Camera, Sensor, Site, load_site
from .agents import Agent, Population, Truck

# Real-world heights in metres, used to size a detection box from its distance. Values a surveyor
# would recognise, because the point of a physical projection model is that both sides agree on
# physics rather than on a curve that looks about right.
OBJECT_HEIGHT_M: dict[EntityType, float] = {
    EntityType.TRUCK: 3.6,
    EntityType.VEHICLE: 1.6,
    EntityType.FORKLIFT: 2.3,
    EntityType.PERSON: 1.75,
    EntityType.DRONE: 0.5,
}


@dataclass
class TickOutput:
    """Everything one simulator tick wants published, grouped by topic."""

    observations: list[tuple[str, Observation]] = field(default_factory=list)
    entities: list[Any] = field(default_factory=list)

    def add(self, topic: str | Topic, observation: Observation) -> None:
        self.observations.append((str(topic), observation))

    def __len__(self) -> int:
        return len(self.observations) + len(self.entities)


@dataclass
class Incident:
    """An injected condition that changes what sensors report."""

    kind: str
    zone_id: str
    started_at: float
    duration_s: float = 600.0
    intensity: float = 1.0

    def active(self, now: float) -> bool:
        return self.started_at <= now <= self.started_at + self.duration_s


class YardSimulator:
    """Deterministic, seeded simulation of the demo site."""

    def __init__(
        self,
        *,
        site: Site | None = None,
        seed: int = 1337,
        trucks: int = 6,
        forklifts: int = 3,
        people: int = 8,
        drones: int = 1,
        frame_fps: float = 2.0,
        gps_hz: float = 1.0,
        sensor_hz: float = 0.2,
    ) -> None:
        self.site = site or load_site()
        self.rng = random.Random(seed)
        # Publish rates are deliberately decoupled from the tick rate. Motion is stepped often
        # (smooth movement on the map) while each *source* reports at a realistic cadence: a GPS
        # tracker at 1 Hz, a temperature sensor every five seconds. Publishing every source on every
        # tick tripled the bus rate for no extra information, and pushed the pipeline past the PRD's
        # 10-50 events/second envelope while telling us nothing new about the yard.
        self.frame_interval_s = 1.0 / frame_fps if frame_fps > 0 else 0.5
        self.gps_interval_s = 1.0 / gps_hz if gps_hz > 0 else 1.0
        self.sensor_interval_s = 1.0 / sensor_hz if sensor_hz > 0 else 5.0
        self.population = Population(
            self.site, self.rng, trucks=trucks, forklifts=forklifts, people=people, drones=drones
        )
        self.incidents: list[Incident] = []
        # A single internal clock, advanced only by step(dt), drives everything: agent motion,
        # frame rate gating and incident ramps. Mixing it with time.monotonic() would make the
        # simulator non-deterministic — stepping 60 simulated seconds in a test would advance the
        # agents a minute while the fire ramp and the camera frame rate believed no time had
        # passed. In production the service passes real elapsed time, so behaviour is identical.
        self.clock = 0.0
        self.wall_started_at = time.monotonic()
        # Wall-clock anchor for the internal clock, so simulation seconds can be reported as real
        # timestamps.
        self.started_utc = utc_now()
        # Run identity. Agent ids restart from 0001 every run, so without this a restart would
        # resurrect the previous run's entities and inherit their lifetimes through the store's
        # merge — trucks reported an hour on site moments after boot.
        self.run_id = new_id("run").split("_", 1)[-1][-6:].lower()
        self._last_frame_at: dict[str, float] = {}
        self._last_gps_at: dict[str, float] = {}
        self._last_sensor_at: dict[str, float] = {}
        self._frame_counter = 0
        self._rfid_seen: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------- helpers
    @property
    def elapsed_s(self) -> float:
        """Simulated seconds since start."""
        return self.clock

    @property
    def wall_elapsed_s(self) -> float:
        return time.monotonic() - self.wall_started_at

    def inject_fire(self, zone_id: str = "dock_3", *, duration_s: float = 900.0) -> Incident:
        """Start a fire. Raises the local temperature sensor and marks frames from covering cameras.

        The demo's headline scenario (UC2). Kept as an explicit injection because a fire that
        happens randomly is impossible to narrate and impossible to test.
        """
        incident = Incident("fire", zone_id, self.clock, duration_s, intensity=1.0)
        self.incidents.append(incident)
        return incident

    def inject_power_failure(self, duration_s: float = 300.0) -> Incident:
        incident = Incident("power_failure", "office", self.clock, duration_s)
        self.incidents.append(incident)
        return incident

    def active_incidents(self) -> list[Incident]:
        now = self.clock
        self.incidents = [i for i in self.incidents if i.active(now) or i.started_at > now]
        return [i for i in self.incidents if i.active(now)]

    def incident_in(self, zone_id: str, kind: str = "fire") -> Incident | None:
        return next(
            (i for i in self.active_incidents() if i.kind == kind and i.zone_id == zone_id), None
        )

    # --------------------------------------------------------------------- ticks
    def step(self, dt: float) -> TickOutput:
        """Advance the simulation by ``dt`` seconds and return what to publish."""
        self.clock += dt
        now = self.clock
        self.population.step(dt, now)
        output = TickOutput()

        for agent in self.population.active_agents():
            if agent.has_gps and self._due(self._last_gps_at, agent.agent_id, self.gps_interval_s):
                output.add(Topic.RAW_GPS, self._gps_observation(agent))
            self._rfid_observations(agent, output)

        for camera in self.site.cameras:
            observation = self._frame_observation(camera, now)
            if observation is not None:
                output.add(Topic.RAW_FRAMES, observation)

        for sensor in self.site.sensors:
            if not self._due(self._last_sensor_at, sensor.source_id, self.sensor_interval_s):
                continue
            observation = self._sensor_observation(sensor, now)
            if observation is not None:
                output.add(Topic.RAW_IOT, observation)

        return output

    def now_utc(self) -> datetime:
        """Current simulated time as a real timestamp.

        Every timestamp the simulator emits comes from here, never from ``utc_now()`` directly.
        Mixing the two is subtly wrong: a test that advances five simulated minutes in half a second
        of wall time would produce entities whose first_seen is five minutes old and whose last_seen
        is *now*, or vice versa. Anchoring everything to the internal clock means simulated durations
        are exactly what the simulation says they are — and in production, where dt is real elapsed
        time, the two are identical anyway.
        """
        return self.started_utc + timedelta(seconds=self.clock)

    def _due(self, ledger: dict[str, float], key: str, interval_s: float) -> bool:
        """Rate-gate one source. Records the time only when it fires."""
        if self.clock - ledger.get(key, -interval_s) < interval_s:
            return False
        ledger[key] = self.clock
        return True

    # -------------------------------------------------------------- observations
    def _gps_observation(self, agent: Agent) -> Observation:
        geo = agent.gps_reading()
        return Observation(
            source_id=f"gps-{agent.agent_id}",
            modality=Modality.GPS,
            ts=self.now_utc(),
            geo=geo,
            confidence=0.9,
            payload={
                "agent_id": agent.agent_id,
                "entity_type": str(agent.entity_type),
                "label": agent.label,
                "speed_mps": round(agent.kinematics.speed_mps, 2),
                "heading_deg": round(agent.kinematics.heading_deg, 1),
                "zone_id": agent.zone_id,
                # Horizontal accuracy is what fusion should weight by, so report it honestly
                # rather than implying a perfect fix.
                "hdop_m": 2.0,
                **agent.state_summary(),
            },
        )

    def _rfid_observations(self, agent: Agent, output: TickOutput) -> None:
        """A gate RFID reader fires once per truck per gate, not continuously."""
        if not isinstance(agent, Truck):
            return
        for reader, zone_id in (("iot-rfid-gate-a", "gate_a"), ("iot-rfid-gate-b", "gate_b")):
            if agent.zone_id != zone_id:
                continue
            key = (reader, agent.agent_id)
            if key in self._rfid_seen:
                continue
            self._rfid_seen.add(key)
            zone = self.site.zone(zone_id)
            centroid = zone.centroid if zone else (0.0, 0.0)
            from ..site import to_geo

            output.add(
                Topic.RAW_IOT,
                Observation(
                    source_id=reader,
                    modality=Modality.RFID,
                    ts=self.now_utc(),
                    geo=to_geo(*centroid),
                    confidence=0.99,
                    payload={
                        "metric": "rfid_read",
                        "tag_id": f"TAG-{agent.plate}",
                        "plate": agent.plate,
                        "agent_id": agent.agent_id,
                        "zone_id": zone_id,
                        "value": 1,
                    },
                ),
            )

    def _frame_observation(self, camera: Camera, now: float) -> Observation | None:
        """A frame from one camera, listing what is genuinely inside its field of view.

        Frames are only emitted when the camera can see something. A yard camera staring at empty
        tarmac at 2 fps would be most of the bus traffic and none of the value; real deployments
        use motion gating for exactly the same reason.
        """
        last = self._last_frame_at.get(camera.source_id, 0.0)
        if now - last < self.frame_interval_s:
            return None

        visible = [
            agent
            for agent in self.population.active_agents()
            if camera.sees(agent.kinematics.east, agent.kinematics.north)
        ]
        fire = next(
            (i for i in self.active_incidents() if i.kind == "fire" and i.zone_id in camera.covers),
            None,
        )
        if not visible and fire is None:
            return None

        self._last_frame_at[camera.source_id] = now
        self._frame_counter += 1
        frame_id = new_id("frm")

        return Observation(
            source_id=camera.source_id,
            modality=Modality.VIDEO,
            ts=self.now_utc(),
            geo=camera.geo,
            confidence=1.0,
            # Phase 2 replaces this with a real object key written to MinIO; the key shape is
            # already the one perception will use, so nothing downstream changes.
            raw_ref=f"frames/{camera.source_id}/{frame_id}.jpg",
            payload={
                "frame_id": frame_id,
                "width": 1280,
                "height": 720,
                "sequence": self._frame_counter,
                "camera": {
                    "bearing_deg": camera.bearing_deg,
                    "fov_deg": camera.fov_deg,
                    "range_m": camera.range_m,
                    "covers": list(camera.covers),
                },
                # Ground truth for what is in shot. Phase 2's perception service ignores this and
                # runs a real detector; until then it lets the pipeline be exercised end to end,
                # and afterwards it is what the detection eval harness scores against — which is
                # why an unprojectable box is dropped rather than emitted as null: ground truth
                # containing a box that is not in the frame would poison the mAP numbers.
                "visible": self._visible_payload(camera, visible),
                "fire": bool(fire),
                "simulated": True,
            },
        )

    @staticmethod
    def _detector_class(entity_type: EntityType) -> str:
        """Map a world-model type to the class name a detector would emit.

        They are not the same vocabulary: COCO says "person" and "truck" but has no "forklift", so
        a forklift is detected as a truck and only becomes a forklift after fusion adds context.
        Pretending otherwise would make the Phase 2 eval numbers meaningless.
        """
        return {
            EntityType.TRUCK: "truck",
            EntityType.FORKLIFT: "truck",
            EntityType.PERSON: "person",
            EntityType.DRONE: "airplane",
            EntityType.VEHICLE: "car",
        }.get(entity_type, "unknown")

    def _visible_payload(self, camera: Camera, visible: list[Agent]) -> list[dict[str, Any]]:
        """Ground-truth entries for the agents this camera can actually frame."""
        entries: list[dict[str, Any]] = []
        for agent in visible:
            bbox = self._project_bbox(camera, agent)
            if bbox is None:
                continue
            entries.append(
                {
                    "agent_id": agent.agent_id,
                    "class": self._detector_class(agent.entity_type),
                    "label": agent.label,
                    "bbox": bbox,
                    "distance_m": round(
                        math.hypot(
                            agent.kinematics.east - camera.east,
                            agent.kinematics.north - camera.north,
                        ),
                        1,
                    ),
                }
            )
        return entries

    @staticmethod
    def _project_bbox(camera: Camera, agent: Agent) -> list[float] | None:
        """Project an agent into the image with an actual pinhole model.

        Replaced two hand-tuned curves — one here and one in fusion's ground projector — that
        disagreed by 10 to 28 metres, far outside any sensible association gate. Using the *same
        physics* on both sides makes the inverse exact, and that agreement is what calibration means:
        here the simulator stands in for the physical world, and fusion reads the same declared
        camera pose from the database.

        An airborne object is still projected by its ground track. The flat-ground assumption does not
        hold for it, and fusion is told to distrust camera fixes for drones rather than silently
        believing a position that is wrong by the drone's altitude.
        """
        width_px, height_px = 1280.0, 720.0
        dx = agent.kinematics.east - camera.east
        dy = agent.kinematics.north - camera.north
        distance = max(1.0, math.hypot(dx, dy))

        # --- horizontal: bearing offset maps to a column ----------------------------
        bearing = (math.degrees(math.atan2(dx, dy)) + 360) % 360
        offset_deg = (bearing - camera.bearing_deg + 180) % 360 - 180
        centre_x = width_px / 2 + (offset_deg / (camera.fov_deg / 2)) * (width_px / 2)

        # --- vertical: the ground contact point maps to a row -----------------------
        # A ground point at horizontal distance d from a camera at height h has depression angle
        # atan(h/d); with the axis tilted tilt_deg below horizontal over a vertical field of view
        # vfov_deg, that is linear in image row.
        depression_deg = math.degrees(math.atan(camera.height_m / distance))
        ground_y = height_px / 2 + ((depression_deg - camera.tilt_deg) / (camera.vfov_deg / 2)) * (
            height_px / 2
        )

        # --- apparent height is the angle the object subtends ------------------------
        object_height_m = OBJECT_HEIGHT_M.get(agent.entity_type, 1.8)
        subtended_deg = math.degrees(math.atan(object_height_m / distance))
        box_height = (subtended_deg / camera.vfov_deg) * height_px
        if agent.entity_type in (EntityType.TRUCK, EntityType.VEHICLE):
            aspect = 2.1
        elif agent.entity_type is EntityType.FORKLIFT:
            aspect = 1.4
        else:
            aspect = 0.45
        box_width = box_height * aspect

        x1 = max(0.0, min(width_px, centre_x - box_width / 2))
        y1 = max(0.0, min(height_px, ground_y - box_height))
        x2 = max(0.0, min(width_px, centre_x + box_width / 2))
        y2 = max(0.0, min(height_px, ground_y))
        if x2 - x1 < 2.0 or y2 - y1 < 2.0:
            return None  # clipped away entirely, or too small to count as a detection
        return [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)]

    def _sensor_observation(self, sensor: Sensor, now: float) -> Observation | None:
        """A fixed-sensor reading, perturbed by any active incident in its zone."""
        if sensor.metric in ("rfid_read",):
            return None  # event-driven, handled in _rfid_observations

        value = sensor.baseline + self.rng.gauss(0, sensor.noise)
        attributes: dict[str, Any] = {}

        fire = self.incident_in(sensor.zone_id or "", "fire") if sensor.zone_id else None
        if fire is not None and sensor.metric == "temperature_c":
            # Ramp rather than jump: a step change is trivially detectable and would flatter the
            # anomaly detector. A ramp is what a real fire looks like to a thermometer.
            minutes = (now - fire.started_at) / 60.0
            value += min(70.0, 14.0 * minutes) * fire.intensity
            attributes["incident"] = "fire"

        if sensor.metric == "door_open":
            worker_present = any(
                agent.zone_id == sensor.zone_id
                for agent in self.population.active_agents()
                if agent.entity_type is EntityType.PERSON
            )
            value = 1.0 if worker_present else 0.0

        if sensor.metric == "power_kw":
            outage = self.incident_in("office", "power_failure")
            if outage is not None:
                value = self.rng.uniform(0.0, 2.0)
                attributes["incident"] = "power_failure"
            else:
                # Daily-ish load curve, so the forecaster has structure to find rather than noise.
                value += 40.0 * math.sin(self.elapsed_s / 240.0)

        return Observation(
            source_id=sensor.source_id,
            modality=Modality.IOT,
            ts=self.now_utc(),
            geo=sensor.geo,
            confidence=0.97,
            payload={
                "metric": sensor.metric,
                "value": round(value, 2),
                "unit": sensor.unit,
                "zone_id": sensor.zone_id,
                "label": sensor.label,
                **attributes,
            },
        )

    # ------------------------------------------------------------------ entities
    def ground_truth_entities(self) -> list[Any]:
        """Simulated entities for the world model — the Phase 1 bridge described in the module docstring."""
        from sio_schemas import Entity

        entities: list[Entity] = []
        now = self.now_utc()
        for agent in self.population.active_agents():
            zone_id = agent.zone_id
            # A real first_seen, from when the agent appeared. Letting pydantic default it stamped
            # first_seen = last_seen = now on every tick, so every dwell time computed by a consumer
            # of the live stream was zero — including the UI's entity panel, which is exactly where
            # UC1 ("stayed more than 15 minutes") has to be visible.
            first_seen = self.started_utc + timedelta(seconds=agent.spawned_at)
            entities.append(
                Entity(
                    entity_id=f"sim-{self.run_id}-{agent.agent_id}",
                    type=agent.entity_type,
                    label=agent.label,
                    first_seen=first_seen,
                    last_seen=now,
                    state=EntityState(
                        ts=self.now_utc(),
                        geo=agent.kinematics.geo,
                        velocity=agent.kinematics.velocity,
                        heading_deg=agent.kinematics.heading_deg,
                        zone_id=zone_id,
                        confidence=0.95,
                    ),
                    provenance=[
                        Provenance(
                            source_id=f"gps-{agent.agent_id}" if agent.has_gps else "simulator",
                            modality=Modality.GPS if agent.has_gps else Modality.MANUAL,
                            ts=self.now_utc(),
                            confidence=0.9,
                            note="simulated ground truth (Phase 1 bridge)",
                        )
                    ],
                    confidence=0.95,
                    attributes={"simulated": True, **agent.state_summary()},
                )
            )
        return entities

    def site_entities(self) -> list[Any]:
        """The fixed cast: cameras, sensors, gates, docks, zones-as-entities.

        Emitted once at startup. They are entities, not just map decoration, because the copilot
        needs to traverse to them ("which camera covers Gate B", "the temperature sensor in dock 3")
        and because events reference them.
        """
        from sio_schemas import Entity

        entities: list[Entity] = []
        now = self.now_utc()

        for camera in self.site.cameras:
            entities.append(
                Entity(
                    entity_id=f"sim-{camera.source_id}",
                    type=EntityType.CAMERA,
                    label=camera.label or camera.source_id,
                    is_static=True,
                    state=EntityState(ts=now, geo=camera.geo, confidence=1.0),
                    attributes={
                        "source_id": camera.source_id,
                        "bearing_deg": camera.bearing_deg,
                        "fov_deg": camera.fov_deg,
                        "range_m": camera.range_m,
                        "covers": list(camera.covers),
                        "simulated": True,
                    },
                    confidence=1.0,
                )
            )

        for sensor in self.site.sensors:
            entities.append(
                Entity(
                    entity_id=f"sim-{sensor.source_id}",
                    type=EntityType.SENSOR,
                    label=sensor.label or sensor.source_id,
                    is_static=True,
                    state=EntityState(
                        ts=now, geo=sensor.geo, zone_id=sensor.zone_id, confidence=1.0
                    ),
                    attributes={
                        "source_id": sensor.source_id,
                        "metric": sensor.metric,
                        "unit": sensor.unit,
                        "simulated": True,
                    },
                    confidence=1.0,
                )
            )

        from ..site import to_geo

        for zone in self.site.zones:
            if zone.kind not in ("gate", "dock"):
                continue
            entities.append(
                Entity(
                    entity_id=f"sim-{zone.zone_id}",
                    type=EntityType.GATE if zone.kind == "gate" else EntityType.DOCK,
                    label=zone.name,
                    is_static=True,
                    state=EntityState(
                        ts=now, geo=to_geo(*zone.centroid), zone_id=zone.zone_id, confidence=1.0
                    ),
                    attributes={
                        "zone_id": zone.zone_id,
                        "capacity": zone.capacity,
                        "simulated": True,
                    },
                    confidence=1.0,
                )
            )
        return entities

    # --------------------------------------------------------------------- stats
    def stats(self) -> dict[str, Any]:
        return {
            "elapsed_s": round(self.elapsed_s, 1),
            "wall_elapsed_s": round(self.wall_elapsed_s, 1),
            "agents": self.population.counts(),
            "frames": self._frame_counter,
            "incidents": [
                {"kind": i.kind, "zone": i.zone_id, "age_s": round(self.clock - i.started_at)}
                for i in self.active_incidents()
            ],
            "trucks_docked": sum(
                1
                for agent in self.population.active_agents()
                if isinstance(agent, Truck) and str(agent.state) == "docked"
            ),
        }

    def geo_of(self, agent_id: str) -> Geo | None:
        agent = self.population.by_id(agent_id)
        return agent.kinematics.geo if agent else None
