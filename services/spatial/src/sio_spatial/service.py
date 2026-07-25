"""Spatial service: zone membership, coverage, blind spots (PRD M6).

Consumes fused entities, decides which zones they are in, and asserts that as two things:

* an **event** (`zone_entered` / `zone_exited`) — something happened, append-only;
* a **bitemporal relationship** (`located_in`, opened on entry and closed on exit) — something *is*
  true for an interval.

Both, because they answer different questions. "What happened at 14:32?" reads events; "where was the
truck at 14:32?" reads the edge. Deriving either from the other after the fact is possible and
miserable, and the second one is what makes UC5 replay work.

This service is the single authority on site geometry. Nothing else does point-in-polygon, so there is
one implementation to be right or wrong.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from sio_core import MessageContext, PgPool, SioService, get_pg_pool
from sio_core.explain import ExplanationBuilder
from sio_schemas import (
    BusMessage,
    Entity,
    Event,
    EventType,
    EvidenceKind,
    Geo,
    Relationship,
    RelationshipType,
    Severity,
    Topic,
    utc_now,
)

from .geometry import DEFAULT_H3_RESOLUTION, ZoneIndex, cell_for, cells_within, haversine_m
from .membership import MembershipChange, MembershipTracker
from .queries import SpatialQueries


class SpatialService(SioService):
    """Answers where things are, and notices when that changes."""

    name = "spatial"
    subscribes = (Topic.ENTITIES,)
    tick_interval_s = 10.0

    ZONE_REFRESH_S = 300.0
    """Zones change rarely, but a site being commissioned changes them all afternoon."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.pool: PgPool = get_pg_pool(self.settings)
        self.index = ZoneIndex()
        self.tracker = MembershipTracker(self.index)
        self.queries: SpatialQueries | None = None
        self._open_edges: dict[tuple[str, str], Relationship] = {}
        self._zones_loaded_at = 0.0
        self._events_published = 0
        self._edges_opened = 0
        self._edges_closed = 0
        self._entities_seen = 0
        self._expired = 0
        self._labels: dict[str, str] = {}
        """Last known label per entity, so an inferred exit can name what left.

        An expiry has no Entity to hand — nothing reported it, which is the whole point — so without a
        cache the event reads "ent_01KYBNWZZR1DWFBMPR3ZSW30CW is no longer tracked in Fuel store". That
        is technically complete and useless to the person reading the feed.
        """

    async def setup(self) -> None:
        await self.pool.open()
        self.queries = SpatialQueries(self.pool, self.settings.tenant_id)
        await self._refresh_zones()
        self.tracker.margin_m = self.settings.spatial_boundary_margin_m
        self.tracker.enter_confirm_s = self.settings.spatial_enter_confirm_s
        self.tracker.exit_grace_s = self.settings.spatial_exit_grace_s
        self.log.info(
            "spatial.ready",
            zones=len(self.index),
            margin_m=self.tracker.margin_m,
            enter_confirm_s=self.tracker.enter_confirm_s,
            exit_grace_s=self.tracker.exit_grace_s,
            h3_resolution=self.settings.h3_resolution,
        )

    async def _refresh_zones(self) -> None:
        assert self.queries is not None
        zones = await self.queries.load_zones()
        self.index.replace(zones)
        self._zones_loaded_at = time.monotonic()
        if not zones:
            self.log.warning(
                "spatial.no_zones",
                effect="no membership events will fire",
                hint="run: just seed",
            )

    async def health_checks(self) -> dict[str, str]:
        return {
            "postgres": "ok" if await self.pool.ping() else "unreachable",
            "zones": f"ok ({len(self.index)} zones)"
            if len(self.index)
            else "no zones (run: just seed)",
        }

    async def health_info(self) -> dict[str, str]:
        occupancy = self.tracker.occupancy()
        return {
            "entities_seen": str(self._entities_seen),
            "memberships": str(len(self.tracker.memberships)),
            "occupied_zones": str(len(occupancy)),
            "events_published": str(self._events_published),
            "edges_open": str(len(self._open_edges)),
            "edges_closed": str(self._edges_closed),
            "memberships_expired": str(self._expired),
        }

    # ------------------------------------------------------------------ handling
    async def on_message(self, message: BusMessage, ctx: MessageContext) -> None:
        if message.kind != "Entity":
            return
        entity = message.decode(Entity)
        if entity.is_static or entity.state is None or entity.state.geo is None:
            # Static fixtures have a fixed zone recorded at seed time; re-deriving it on every message
            # would emit an entry event for every camera on the site each time one is republished.
            return
        self._entities_seen += 1
        if entity.label:
            self._labels[entity.entity_id] = entity.label
        changes = self.tracker.observe(entity.entity_id, entity.state.geo, entity.state.ts)
        for change in changes:
            await self._publish_change(change, entity, ctx)

    async def _publish_change(
        self, change: MembershipChange, entity: Entity, ctx: MessageContext | None
    ) -> None:
        """Turn a confirmed transition into an event and open or close its edge."""
        zone = self.index.get(change.zone_id)
        restricted = change.restricted

        if change.kind == "entered":
            event_type = EventType.UNAUTHORIZED_ENTRY if restricted else EventType.ZONE_ENTERED
            severity = Severity.HIGH if restricted else Severity.INFO
        else:
            event_type = EventType.ZONE_EXITED
            severity = Severity.INFO

        explanation = ExplanationBuilder(
            summary=(
                f"{entity.label or entity.entity_id} "
                f"{'entered' if change.kind == 'entered' else 'left'} {change.zone_name}"
            )
        )
        explanation.add_rule(
            f"spatial.zone_{change.kind}", note="geometric membership with hysteresis"
        )
        if change.kind == "entered":
            # One note, not two. Split across two bullets the second one opened with "and", which reads
            # as a formatting accident in a list the operator is meant to trust — and a note is a
            # standalone claim, not a clause.
            explanation.add_note(
                f"position was inside the {change.zone_name} polygon by more than "
                f"{self.tracker.margin_m:.0f} m, and held there for "
                f"{self.tracker.enter_confirm_s:.0f} s"
            )
            explanation.add_note(
                "the hold is required so a vehicle clipping the corner while turning does not count as "
                "an entry"
            )
        else:
            explanation.add_note(
                f"position was outside {change.zone_name} by more than "
                f"{self.tracker.margin_m:.0f} m for {self.tracker.exit_grace_s:.0f} s, "
                "which is long enough to rule out a dropped fix"
            )
            explanation.add_note(f"dwelled {change.dwell_s / 60:.1f} min")
        if restricted:
            explanation.add_note(f"{change.zone_name} is marked restricted")
        explanation.add_entity(entity)
        for provenance in entity.provenance[-3:]:
            explanation.add_evidence(
                EvidenceKind.OBSERVATION,
                provenance.observation_id or provenance.track_id or provenance.source_id,
                ts=provenance.ts,
                source_id=provenance.source_id,
                note=f"{provenance.modality} fix",
            )

        event = Event(
            tenant_id=entity.tenant_id,
            type=event_type,
            severity=severity,
            entities=[entity.entity_id],
            geo=change.geo,
            zone_id=change.zone_id,
            ts=change.ts,
            detected_ts=utc_now(),
            confidence=round(min(0.99, entity.confidence), 3),
            explanation=explanation.build(),
            rule_id=f"spatial.zone_{change.kind}",
            source_ids=sorted({p.source_id for p in entity.provenance[-6:]}),
            attributes={
                "zone_name": change.zone_name,
                "zone_kind": zone.kind if zone else "area",
                "restricted": restricted,
                "dwell_s": round(change.dwell_s, 1),
                "entity_type": str(entity.type),
            },
        )
        await self._emit(Topic.EVENTS, event, ctx)
        self._events_published += 1

        key = (change.entity_id, change.zone_id)
        if change.kind == "entered":
            # One edge per visit, not two.
            #
            # `ENTERED` with a bounded validity interval *is* the visit: it opens at entry and closes
            # at exit, so "where was the truck at 14:32?" is a single interval-overlap query. Emitting
            # a separate `EXITED` edge would record the same fact twice and leave a reader to pair them
            # up — and pairing them is only unambiguous while nothing has been lost, which is precisely
            # the assumption a bitemporal store exists to avoid making.
            relationship = Relationship(
                tenant_id=entity.tenant_id,
                **{"from": change.entity_id, "to": change.zone_id},
                type=RelationshipType.ENTERED,
                ts_valid_from=change.ts,
                confidence=0.95,
                evidence=[event.event_id],
                attributes={"zone_name": change.zone_name, "restricted": restricted},
            )
            self._open_edges[key] = relationship
            self._edges_opened += 1
            await self._emit(Topic.ENTITIES, relationship, ctx)
        else:
            relationship = self._open_edges.pop(key, None)
            if relationship is None:
                # No open edge: the entry happened before this process started. Skipping is right —
                # inventing an edge with a fabricated start would corrupt the very history that
                # bitemporal storage exists to protect.
                self.log.debug(
                    "spatial.exit_without_open_edge", entity=change.entity_id, zone=change.zone_id
                )
                return
            closed = relationship.model_copy(update={"ts_valid_to": change.ts})
            self._edges_closed += 1
            await self._emit(Topic.ENTITIES, closed, ctx)

    async def _publish_expiry(self, change: MembershipChange) -> None:
        """Record an exit inferred from silence, and close its edge.

        Deliberately a distinct path from an observed exit, because it is a distinct fact. Nothing saw
        this entity leave; it simply stopped being reported, and the honest record says so and timestamps
        the departure at the last moment anything was actually known.
        """
        zone = self.index.get(change.zone_id)
        name = self._labels.get(change.entity_id, change.entity_id)
        explanation = ExplanationBuilder(
            summary=f"{name} is no longer tracked in {change.zone_name}"
        )
        explanation.add_rule("spatial.membership_expired", note="exit inferred from silence")
        explanation.add_note(
            f"nothing has reported this entity for {self.settings.spatial_max_silence_s:.0f}s, so its "
            f"membership of {change.zone_name} was closed"
        )
        explanation.add_note(
            "the exit is timestamped at the last confirmed sighting, not now: that is the last moment "
            "anything was actually known"
        )
        explanation.add_note(
            "it may have left, or its tracker may have failed — the two are indistinguishable from here"
        )
        event = Event(
            tenant_id=self.settings.tenant_id,
            type=EventType.ZONE_EXITED,
            severity=Severity.INFO,
            entities=[change.entity_id],
            zone_id=change.zone_id,
            ts=change.ts,
            detected_ts=utc_now(),
            # Lower than an observed exit, because it is an inference rather than an observation.
            confidence=0.6,
            explanation=explanation.build(),
            rule_id="spatial.membership_expired",
            attributes={
                "zone_name": change.zone_name,
                "zone_kind": zone.kind if zone else "area",
                "restricted": change.restricted,
                "dwell_s": round(change.dwell_s, 1),
                "inferred": True,
            },
        )
        await self._emit(Topic.EVENTS, event, None)
        self._events_published += 1
        self._expired += 1

        if not self.tracker.zones_of(change.entity_id):
            # No memberships left: drop the label. Entity ids are minted per run, so an unbounded cache
            # keyed by them is a slow leak.
            self._labels.pop(change.entity_id, None)

        relationship = self._open_edges.pop((change.entity_id, change.zone_id), None)
        if relationship is None:
            return
        closed = relationship.model_copy(update={"ts_valid_to": change.ts})
        self._edges_closed += 1
        await self._emit(Topic.ENTITIES, closed, None)
        self.log.info(
            "spatial.membership_expired",
            entity=change.entity_id,
            zone=change.zone_id,
            dwell_s=round(change.dwell_s, 1),
        )

    async def _emit(self, topic: str, payload: Any, ctx: MessageContext | None) -> None:
        if ctx is not None:
            await ctx.publish(topic, payload)
        else:
            await self.publish(topic, payload)

    async def tick(self) -> None:
        if time.monotonic() - self._zones_loaded_at > self.ZONE_REFRESH_S:
            await self._refresh_zones()
        for change in self.tracker.expire_stale(utc_now(), self.settings.spatial_max_silence_s):
            # Publish it. The first version computed the exit and threw it away — logging a line and
            # popping the edge without telling anyone.
            #
            # The consequence was invisible here and severe two services downstream. Large enclosing
            # zones are only ever *left* by expiry: an entity inside the site is inside the perimeter and
            # the yard until it stops being observed. So the perimeter accumulated 54 entries and ZERO
            # exits, every one of its edges stayed open forever, and the prediction service — which
            # reconstructs occupancy from those edges — reported 41 entities on a dock apron that holds a
            # handful, then forecast it rising.
            await self._publish_expiry(change)
        occupancy = self.tracker.occupancy()
        self.log.info(
            "spatial.stats",
            zones=len(self.index),
            entities_seen=self._entities_seen,
            memberships=len(self.tracker.memberships),
            occupied=len(occupancy),
            events=self._events_published,
            edges_open=len(self._open_edges),
            **{f"stat_{key}": value for key, value in self.tracker.stats.items()},
        )

    # -------------------------------------------------------------------- routes
    def routes(self, app: FastAPI) -> None:
        @app.get("/spatial/zones", tags=["spatial"])
        async def zones() -> dict[str, Any]:
            """Zones with their current confirmed occupancy."""
            occupancy = self.tracker.occupancy()
            return {
                "zones": [
                    {
                        "zone_id": zone.zone_id,
                        "name": zone.name,
                        "kind": zone.kind,
                        "restricted": zone.restricted,
                        "capacity": zone.capacity,
                        "centroid": {"lat": zone.centroid.lat, "lon": zone.centroid.lon},
                        "occupants": occupancy.get(zone.zone_id, []),
                        "occupancy": len(occupancy.get(zone.zone_id, [])),
                        "over_capacity": bool(
                            zone.capacity and len(occupancy.get(zone.zone_id, [])) > zone.capacity
                        ),
                    }
                    for zone in self.index.zones
                ]
            }

        @app.get("/spatial/within", tags=["spatial"])
        async def within(
            lat: float,
            lon: float,
            radius_m: float = Query(500.0, gt=0, le=100_000),
            entity_type: str | None = None,
        ) -> dict[str, Any]:
            """Entities within a radius — the "trucks within 500 m" query."""
            assert self.queries is not None
            results = await self.queries.within_radius(
                Geo(lat=lat, lon=lon), radius_m, entity_type=entity_type
            )
            return {
                "origin": {"lat": lat, "lon": lon},
                "radius_m": radius_m,
                "count": len(results),
                "results": results,
            }

        @app.get("/spatial/nearest", tags=["spatial"])
        async def nearest(
            lat: float,
            lon: float,
            entity_type: str | None = None,
            limit: int = Query(5, ge=1, le=50),
        ) -> dict[str, Any]:
            """Nearest entities of a type — the "nearest hospital" query."""
            assert self.queries is not None
            return {
                "results": await self.queries.nearest(
                    Geo(lat=lat, lon=lon), entity_type=entity_type, limit=limit
                )
            }

        @app.get("/spatial/contains/{zone_id}", tags=["spatial"])
        async def contains(zone_id: str) -> dict[str, Any]:
            """Who is in this zone, per PostGIS and per the debounced tracker.

            Both are returned rather than one, because they answer subtly different questions: PostGIS
            gives the instantaneous truth, the tracker gives the *confirmed* truth that events were
            based on. When they disagree it is nearly always an entity mid-confirmation on a boundary,
            and hiding that would make a puzzling event timeline impossible to explain.
            """
            assert self.queries is not None
            if self.index.get(zone_id) is None:
                raise HTTPException(status_code=404, detail=f"unknown zone: {zone_id}")
            postgis = await self.queries.contains(zone_id)
            confirmed = self.tracker.occupancy().get(zone_id, [])
            return {
                "zone_id": zone_id,
                "postgis": [row["entity_id"] for row in postgis],
                "confirmed": confirmed,
                "agree": sorted(row["entity_id"] for row in postgis) == sorted(confirmed),
            }

        @app.get("/spatial/zones_at", tags=["spatial"])
        async def zones_at(lat: float, lon: float) -> dict[str, Any]:
            """Which zones contain a point, innermost first, from both implementations."""
            assert self.queries is not None
            geo = Geo(lat=lat, lon=lon)
            return {
                "point": {"lat": lat, "lon": lon},
                "postgis": await self.queries.zones_at(geo),
                "in_memory": [
                    {
                        "zone_id": zone.zone_id,
                        "name": zone.name,
                        "restricted": zone.restricted,
                        "depth_m": round(zone.distance_to_boundary_m(geo), 2),
                    }
                    for zone in self.index.zones_containing(geo)
                ],
                "h3_cell": cell_for(geo, self.settings.h3_resolution),
            }

        @app.get("/spatial/coverage/{source_id}", tags=["spatial"])
        async def coverage(source_id: str) -> dict[str, Any]:
            """What a camera can see."""
            assert self.queries is not None
            return await self.queries.coverage_of(source_id)

        @app.get("/spatial/cameras_covering/{zone_id}", tags=["spatial"])
        async def cameras_covering(zone_id: str) -> dict[str, Any]:
            """Cameras covering a zone — the "cameras covering Gate B" query."""
            assert self.queries is not None
            return {"zone_id": zone_id, "cameras": await self.queries.cameras_covering(zone_id)}

        @app.get("/spatial/blind_spots", tags=["spatial"])
        async def blind_spots() -> dict[str, Any]:
            """Where on the site no camera can see."""
            assert self.queries is not None
            return await self.queries.blind_spots()

        @app.get("/spatial/density", tags=["spatial"])
        async def density(
            resolution: int = Query(0, ge=0, le=15), active_within_s: float = 900.0
        ) -> dict[str, Any]:
            """Entity counts per H3 cell — where in the yard things actually happen."""
            assert self.queries is not None
            chosen = resolution or self.settings.h3_resolution
            cells = await self.queries.h3_density(
                resolution=chosen, active_within_s=active_within_s
            )
            return {"resolution": chosen, "cells": cells}

        @app.get("/spatial/h3", tags=["spatial"])
        async def h3_cells(
            lat: float,
            lon: float,
            radius_m: float = Query(100.0, gt=0, le=5_000),
            resolution: int = 0,
        ) -> dict[str, Any]:
            """The H3 cells covering a radius, for a client that wants to draw them."""
            chosen = resolution or self.settings.h3_resolution
            geo = Geo(lat=lat, lon=lon)
            cells = cells_within(geo, radius_m, chosen)
            return {
                "origin": {"lat": lat, "lon": lon},
                "radius_m": radius_m,
                "resolution": chosen,
                "count": len(cells),
                "cells": cells,
            }

        @app.get("/spatial/membership/{entity_id}", tags=["spatial"])
        async def membership(entity_id: str) -> dict[str, Any]:
            """Which zones an entity is confirmed to be in, and for how long."""
            zones = self.tracker.zones_of(entity_id)
            return {
                "entity_id": entity_id,
                "zones": [
                    {
                        "zone_id": zone_id,
                        "name": (
                            self.index.get(zone_id).name if self.index.get(zone_id) else zone_id
                        ),
                        "dwell_s": round(self.tracker.dwell_of(entity_id, zone_id) or 0.0, 1),
                    }
                    for zone_id in zones
                ],
            }

        @app.get("/spatial", tags=["spatial"])
        async def describe() -> dict[str, Any]:
            """Membership statistics, including how much hysteresis is suppressing."""
            return {
                "zones": len(self.index),
                "h3_resolution": self.settings.h3_resolution,
                "hysteresis": {
                    "margin_m": self.tracker.margin_m,
                    "enter_confirm_s": self.tracker.enter_confirm_s,
                    "exit_grace_s": self.tracker.exit_grace_s,
                },
                "memberships": len(self.tracker.memberships),
                "occupancy": {
                    zone_id: len(occupants)
                    for zone_id, occupants in self.tracker.occupancy().items()
                },
                "stats": dict(self.tracker.stats),
                "events_published": self._events_published,
                "edges": {"open": len(self._open_edges), "closed": self._edges_closed},
            }


__all__ = ["DEFAULT_H3_RESOLUTION", "SpatialService", "haversine_m"]
