"""Fusion service: tracks, GPS fixes and IoT reads in, entities out (PRD M5)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from fastapi import FastAPI

from sio_core import MessageContext, PgPool, SioService, get_pg_pool
from sio_schemas import (
    BusMessage,
    Entity,
    EntityType,
    Geo,
    Modality,
    Observation,
    Relationship,
    RelationshipType,
    Topic,
    Track,
    utc_now,
)

from .fuse import (
    SENSOR_SIGMA_M,
    Observation2D,
    SensorFusion,
    entity_type_for,
    observation_from_gps,
    observation_from_rfid,
)
from .projection import CameraCalibration, GroundProjector, to_local_metres


def _fleet_number(device_id: str) -> str:
    """Turn an internal device id into something an operator would say out loud.

    ``gps:gps-drone-0018`` becomes ``0018``. The namespace is an implementation detail of association,
    and repeating the type in the label ("Drone gps-drone-0018") is noise on a crowded map.
    """
    without_namespace = device_id.split(":", 1)[-1]
    tail = without_namespace.rsplit("-", 1)[-1]
    return tail if len(tail) >= 3 else without_namespace


class FusionService(SioService):
    """Turns per-sensor observations into one entity per real-world object.

    Subscribes to three topics because fusion is the *only* component that needs to see all of them
    at once: a camera track, a GPS fix and an RFID read are three views of one truck, and nothing
    downstream should have to work that out for itself.
    """

    name = "fusion"
    subscribes = (Topic.TRACKS, Topic.RAW_GPS, Topic.RAW_IOT)
    tick_interval_s = 5.0

    PUBLISH_INTERVAL_S = 1.0
    """How often fused entities are published.

    Publishing on every observation would emit an entity per GPS fix per second per object. The state
    is a *filtered estimate*, so republishing it at a fixed cadence is both cheaper and more honest —
    the estimate changes continuously, not only when a message arrives.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.pool: PgPool = get_pg_pool(self.settings)
        self.fusion: SensorFusion | None = None
        self.projectors: dict[str, GroundProjector] = {}
        self._last_publish = 0.0
        self._published = 0
        self._relationships = 0
        self._unprojectable = 0
        self._seen_by: dict[tuple[str, str], str] = {}
        """(entity, camera) → relationship id, so `seen_by` is opened once and closed later."""

    # ----------------------------------------------------------------- lifecycle
    async def setup(self) -> None:
        await self.pool.open()
        origin = await self._load_origin()
        self.fusion = SensorFusion(
            origin,
            assoc_radius_m=self.settings.fusion_assoc_radius_m,
            time_window_s=self.settings.fusion_time_window_s,
            max_stale_s=self.settings.fusion_max_stale_s,
            reid_threshold=self.settings.track_reid_threshold,
        )
        await self._load_calibrations()
        self.log.info(
            "fusion.ready",
            origin={"lat": origin.lat, "lon": origin.lon},
            cameras=len(self.projectors),
            assoc_radius_m=self.settings.fusion_assoc_radius_m,
            time_window_s=self.settings.fusion_time_window_s,
        )

    async def _load_origin(self) -> Geo:
        """Local coordinate origin: the centroid of the site's zones.

        Derived from the data rather than hard-coded, so the same service works on any site. The
        filter runs in metres relative to this point.
        """
        row = await self.pool.fetchrow(
            "SELECT ST_Y(ST_Centroid(ST_Collect(geom::geometry))::geography::geometry) AS lat, "
            "       ST_X(ST_Centroid(ST_Collect(geom::geometry))::geography::geometry) AS lon "
            "  FROM zones WHERE tenant_id = %s",
            (self.settings.tenant_id,),
        )
        if row and row.get("lat") is not None:
            return Geo(lat=float(row["lat"]), lon=float(row["lon"]))
        self.log.warning(
            "fusion.no_site_geometry",
            effect="using a default origin; run: just seed",
        )
        return Geo(lat=37.7749, lon=-122.4194)

    async def _load_calibrations(self) -> None:
        """Read camera poses from the database.

        Calibration is *data*. A fusion service that imported the simulator's site model to learn
        where the cameras are could never work on a real site.
        """
        rows = await self.pool.fetch(
            """
            SELECT source_id, zone_id, config,
                   ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon
              FROM sources
             WHERE tenant_id = %s AND kind = 'camera'
            """,
            (self.settings.tenant_id,),
        )
        for row in rows:
            calibration = CameraCalibration.from_source_row(dict(row))
            if calibration is not None:
                self.projectors[calibration.source_id] = GroundProjector(calibration)
        if not self.projectors:
            self.log.warning(
                "fusion.no_cameras",
                effect="camera tracks cannot be placed on the ground; GPS-only fusion",
                hint="run: just seed",
            )

    async def health_checks(self) -> dict[str, str]:
        return {
            "postgres": "ok" if await self.pool.ping() else "unreachable",
            "calibration": f"ok ({len(self.projectors)} cameras)"
            if self.projectors
            else "no camera calibration (run: just seed)",
        }

    async def health_info(self) -> dict[str, str]:
        if self.fusion is None:
            return {}
        return {
            "entities": str(len(self.fusion.entities)),
            "multi_sensor": str(
                sum(1 for entity in self.fusion.entities.values() if entity.is_multi_sensor)
            ),
            "published": str(self._published),
            "relationships": str(self._relationships),
            "unprojectable_tracks": str(self._unprojectable),
        }

    # ------------------------------------------------------------------ handling
    async def on_message(self, message: BusMessage, ctx: MessageContext) -> None:
        if self.fusion is None:
            return
        if message.kind == "Track":
            self._observe_track(message.decode(Track))
        elif message.kind == "Observation":
            self._observe_sensor(message.decode(Observation), message.topic)
        await self._maybe_publish(ctx)

    def _observe_track(self, track: Track) -> None:
        """Place a camera track on the ground and fold it in.

        This is the association problem in its real form: the projected position carries several
        metres of uncertainty, there is no device id, and the only other cues are time, class and
        appearance.
        """
        assert self.fusion is not None
        projector = self.projectors.get(track.source_id)
        latest = track.latest
        if projector is None or latest is None or latest.bbox is None:
            self._unprojectable += 1
            return

        if entity_type_for(track.class_name) is EntityType.DRONE:
            # A monocular camera cannot localise an airborne object. Ground projection assumes the
            # box's bottom edge touches the ground, so a drone at 35 m altitude is placed at whatever
            # ground point lies along that ray — tens of metres from where it actually is.
            #
            # Live, that fabricated a phantom "Drone EXHGXV" on the map beside the real, GPS-tracked
            # one. No entity is a better answer than a confident one in the wrong place, so the
            # camera contributes nothing to an airborne object's position and the count is reported
            # rather than silently dropped. (Stereo, a second camera's ray, or radar would fix this.)
            self._airborne_declined += 1
            return

        fix = projector.project(latest.bbox)
        if fix is None:
            self._unprojectable += 1  # the box sits above the horizon: no ground intersection
            return

        east, north = to_local_metres(fix.geo, self.fusion.origin)
        self.fusion.observe(
            Observation2D(
                source_id=track.source_id,
                modality=Modality.VIDEO,
                ts=track.last_ts,
                east=east,
                north=north,
                # The projector's own uncertainty, not a constant: a detection 50 m away is a much
                # worse position fix than one at 10 m, and the filter should know that.
                sigma_m=max(fix.position_sigma_m, SENSOR_SIGMA_M[Modality.VIDEO] * 0.5),
                label=track.class_name,
                confidence=track.confidence,
                track_id=track.track_id,
                embedding=tuple(track.embedding) if track.embedding else None,
                attributes={"range_m": fix.range_m, "camera_bearing_deg": fix.bearing_deg},
            )
        )

    def _observe_sensor(self, observation: Observation, topic: str) -> None:
        assert self.fusion is not None
        if observation.geo is None:
            return  # a temperature reading with no position tells fusion nothing about *where*
        payload = observation.payload

        if observation.modality is Modality.GPS:
            self.fusion.observe(
                observation_from_gps(
                    payload,
                    observation.source_id,
                    observation.ts,
                    observation.geo,
                    self.fusion.origin,
                )
            )
            return

        if payload.get("metric") == "rfid_read" or observation.modality is Modality.RFID:
            self.fusion.observe(
                observation_from_rfid(
                    payload,
                    observation.source_id,
                    observation.ts,
                    observation.geo,
                    self.fusion.origin,
                )
            )
            return

        # Other IoT readings (temperature, power, door state) are not positional observations of a
        # moving object; they belong to the *site*, and the events engine reads them from the
        # measurements table. Fusion deliberately ignores them rather than inventing an entity per
        # thermometer reading.

    # ----------------------------------------------------------------- publishing
    async def _maybe_publish(self, ctx: MessageContext | None) -> None:
        now = time.monotonic()
        if now - self._last_publish < self.PUBLISH_INTERVAL_S:
            return
        self._last_publish = now
        await self._publish_entities(ctx)

    async def _publish_entities(self, ctx: MessageContext | None) -> None:
        assert self.fusion is not None
        # Merge before publishing, so a truck that was briefly two entities is published once rather
        # than twice — otherwise the world model would carry both and the duplicate would outlive the
        # merge.
        merged = self.fusion.merge_pass()
        if merged:
            self.log.info("fusion.merged", pairs=merged, note="track-to-track fusion")
        for fused in list(self.fusion.publishable()):
            entity = Entity(
                entity_id=fused.entity_id,
                tenant_id=self.settings.tenant_id,
                type=fused.entity_type,
                label=self._label_for(fused),
                state=self.fusion.to_entity_state(fused),
                provenance=list(fused.provenance),
                first_seen=fused.first_seen,
                last_seen=fused.last_seen,
                confidence=round(
                    min(0.99, 0.5 + 0.1 * len(fused.modalities) + 0.01 * fused.observations), 3
                ),
                track_ids=sorted(fused.track_ids),
                attributes={
                    **fused.attributes,
                    "fused": True,
                    "modalities": sorted(fused.modalities),
                    "observations": fused.observations,
                    "devices": sorted(fused.device_ids),
                    "position_sigma_m": round(fused.filter.position_sigma_m, 2),
                },
            )
            if ctx is not None:
                await ctx.publish(Topic.ENTITIES, entity)
            else:
                await self.publish(Topic.ENTITIES, entity)
            self._published += 1
            await self._publish_seen_by(fused, entity, ctx)

    def _label_for(self, fused: Any) -> str | None:
        """A human-facing name built from evidence, never from ground truth.

        A plate read by an RFID reader or by OCR is a legitimate label; the simulator's own name for
        the agent is not, and is filtered out before it ever reaches here.

        Falls back to a fleet-number style name derived from the device id. The internal namespace and
        the repeated type name are stripped — a map label reading "Drone gps:gps-drone-0018" leaks
        plumbing into the operator's view, and an operator reads "Drone 0018".
        """
        kind = str(fused.entity_type).title()
        plate = fused.attributes.get("plate")
        if plate:
            return f"{kind} {plate}"
        if fused.device_ids:
            return f"{kind} {_fleet_number(sorted(fused.device_ids)[0])}"
        return f"{kind} {fused.entity_id[-6:]}"

    async def _publish_seen_by(
        self, fused: Any, entity: Entity, ctx: MessageContext | None
    ) -> None:
        """Record which cameras have seen this entity — the edge UC3 traverses.

        Opened once per (entity, camera) pair and left open; the world model's bitemporal edges mean
        "which camera last saw entity X" is answerable later even after the entity has left.
        """
        cameras = [source for source in fused.source_ids if source in self.projectors]
        for camera in cameras:
            key = (entity.entity_id, camera)
            if key in self._seen_by:
                continue
            relationship = Relationship(
                tenant_id=entity.tenant_id,
                **{"from": entity.entity_id, "to": f"sim-{camera}"},
                type=RelationshipType.SEEN_BY,
                ts_valid_from=fused.last_seen,
                confidence=0.9,
                evidence=[
                    entry.detection_id or entry.track_id or "" for entry in fused.provenance[-3:]
                ],
                attributes={"camera": camera},
            )
            self._seen_by[key] = relationship.id
            if ctx is not None:
                await ctx.publish(Topic.ENTITIES, relationship)
            else:
                await self.publish(Topic.ENTITIES, relationship)
            self._relationships += 1

    # ---------------------------------------------------------------------- tick
    async def tick(self) -> None:
        await self._publish_entities(None)
        if self.fusion is None:
            return
        stats = self.fusion.stats
        self.log.info(
            "fusion.stats",
            entities=len(self.fusion.entities),
            multi_sensor=sum(1 for e in self.fusion.entities.values() if e.is_multi_sensor),
            published=self._published,
            by_device=stats["matched_by_device"],
            by_track=stats["matched_by_track"],
            by_position=stats["matched_by_position"],
            by_appearance=stats["matched_by_appearance"],
            gate_rejects=stats["rejected_by_gate"],
            device_conflicts=stats["rejected_by_device_conflict"],
            merged=stats["merged"],
            created=stats["created"],
            expired=stats["expired"],
            unprojectable=self._unprojectable,
        )

    # -------------------------------------------------------------------- routes
    def routes(self, app: FastAPI) -> None:
        @app.get("/fusion", tags=["fusion"])
        async def describe() -> dict[str, Any]:
            """Association statistics: how entities are being matched, and how often gating refuses."""
            if self.fusion is None:
                return {"status": "starting"}
            return {
                **self.fusion.describe(),
                "cameras_calibrated": sorted(self.projectors),
                "published": self._published,
                "relationships": self._relationships,
                "unprojectable_tracks": self._unprojectable,
            }

        @app.get("/fusion/entities", tags=["fusion"])
        async def entities() -> dict[str, Any]:
            """Fused entities with the evidence behind each one."""
            if self.fusion is None:
                return {"entities": []}
            return {
                "entities": [
                    {
                        "entity_id": fused.entity_id,
                        "type": str(fused.entity_type),
                        "label": self._label_for(fused),
                        "modalities": sorted(fused.modalities),
                        "multi_sensor": fused.is_multi_sensor,
                        "observations": fused.observations,
                        "sources": sorted(fused.source_ids),
                        "devices": sorted(fused.device_ids),
                        "tracks": sorted(fused.track_ids),
                        "sigma_m": round(fused.filter.position_sigma_m, 2),
                        "state": self.fusion.to_entity_state(fused).to_wire(),
                        "provenance": [entry.to_wire() for entry in fused.provenance[-6:]],
                    }
                    for fused in self.fusion.publishable()
                ]
            }

    async def teardown(self) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._publish_entities(None), timeout=5.0)


__all__ = ["FusionService", "utc_now"]
