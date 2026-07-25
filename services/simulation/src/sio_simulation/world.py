"""A frozen copy of the world, for asking what would happen (PRD M11, Phase 6).

**A what-if must be counterfactual, and this module is where that is enforced.** The distinction is not
pedantic: the platform already has a tool called `run_simulation` that *injects a fire into the live
simulated site*, which is the opposite of a projection — it makes the thing happen. Asking "what if a gate
closed" and having a gate close is the difference between a forecast and an accident.

So a scenario is handed a `WorldSnapshot`: an immutable, plain-data copy of the site read once at the start
of the run. Nothing a scenario does can reach a store, a bus or a live entity, because it is not given
anything that could. That is a property of the *types*, not of the discipline of whoever writes the next
scenario.

The snapshot also records the instant it was taken. A projection is only meaningful relative to a state of
the world, and one that cannot say which state it started from cannot be checked afterwards — which is the
only way anybody learns whether these numbers are worth anything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

#: Metres per degree of latitude. Good to ~0.1 % anywhere, which is far inside the precision of everything
#: else here — a yard simulation does not need a geodesic.
METRES_PER_DEGREE = 111_320.0


@dataclass(frozen=True)
class SimEntity:
    """One thing on the site, as a scenario sees it."""

    entity_id: str
    type: str
    label: str | None
    lat: float
    lon: float
    zone_id: str | None
    speed_mps: float = 0.0
    battery_pct: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def is_mover(self) -> bool:
        return self.type in ("truck", "forklift", "person", "drone", "vehicle")

    @property
    def is_airborne(self) -> bool:
        return self.type == "drone"

    def distance_to(self, lat: float, lon: float) -> float:
        """Metres, on the equirectangular approximation.

        The cosine correction on longitude matters and is easy to omit: at 51° north a degree of longitude is
        620 m shorter than a degree of latitude, so leaving it out stretches every east-west distance by 60 %
        — which would silently bias every "nearest responder" answer in a yard laid out east to west.
        """
        mean_lat = math.radians((self.lat + lat) / 2)
        dy = (self.lat - lat) * METRES_PER_DEGREE
        dx = (self.lon - lon) * METRES_PER_DEGREE * math.cos(mean_lat)
        return math.hypot(dx, dy)


@dataclass(frozen=True)
class SimZone:
    """A place, with enough geometry to answer "is this in it" and "how far to it"."""

    zone_id: str
    name: str
    kind: str
    lat: float
    lon: float
    restricted: bool = False
    capacity: int | None = None
    polygon: tuple[tuple[float, float], ...] = ()
    """(lon, lat) pairs, as GeoJSON orders them."""

    def contains(self, lat: float, lon: float) -> bool:
        """Ray casting. No Shapely, because a scenario must be runnable without it.

        Shapely is a dependency of the spatial service, not of this one: a what-if that cannot run because an
        optional geometry library is missing is a what-if nobody runs. Ray casting over a yard polygon is
        twenty lines and exact enough for a projection whose inputs are estimates.
        """
        if not self.polygon:
            return False
        inside = False
        count = len(self.polygon)
        for index in range(count):
            x1, y1 = self.polygon[index]
            x2, y2 = self.polygon[(index + 1) % count]
            if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) / (y2 - y1 + 1e-12) * (x2 - x1):
                inside = not inside
        return inside

    @property
    def is_dock(self) -> bool:
        return self.kind == "dock" or self.zone_id.startswith("dock")

    @property
    def is_gate(self) -> bool:
        return self.kind == "gate" or self.zone_id.startswith("gate")


@dataclass(frozen=True)
class WorldSnapshot:
    """The site at one instant, immutable, with nothing live attached.

    `frozen=True` and tuples rather than lists throughout, deliberately. A scenario that could mutate the
    snapshot would produce results that depend on which scenario ran first — and the second run of a
    comparison would silently be measuring something different from the first.
    """

    taken_at: datetime
    entities: tuple[SimEntity, ...]
    zones: tuple[SimZone, ...]
    open_alerts: int = 0
    events_last_hour: int = 0
    tenant_id: str = "default"

    # -- lookups ---------------------------------------------------------------------------
    def zone(self, zone_id: str) -> SimZone | None:
        return next((zone for zone in self.zones if zone.zone_id == zone_id), None)

    def entity(self, entity_id: str) -> SimEntity | None:
        return next((item for item in self.entities if item.entity_id == entity_id), None)

    def in_zone(self, zone_id: str) -> tuple[SimEntity, ...]:
        """Entities in a zone, by recorded membership *or* by geometry.

        Both, because they disagree and each is right sometimes. Recorded membership comes from the spatial
        service's hysteresis, which deliberately lags to avoid event storms; geometry is instantaneous and
        does not know about the margin. For a projection, an entity that is geometrically inside a zone that
        is about to catch fire is affected whether or not the debouncer has caught up.
        """
        zone = self.zone(zone_id)
        return tuple(
            item
            for item in self.entities
            if item.zone_id == zone_id or (zone is not None and zone.contains(item.lat, item.lon))
        )

    def movers(self) -> tuple[SimEntity, ...]:
        return tuple(item for item in self.entities if item.is_mover)

    def of_type(self, *types: str) -> tuple[SimEntity, ...]:
        return tuple(item for item in self.entities if item.type in types)

    def within(self, lat: float, lon: float, radius_m: float) -> tuple[SimEntity, ...]:
        return tuple(item for item in self.entities if item.distance_to(lat, lon) <= radius_m)

    def docks(self) -> tuple[SimZone, ...]:
        return tuple(zone for zone in self.zones if zone.is_dock)

    def describe(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for item in self.entities:
            counts[item.type] = counts.get(item.type, 0) + 1
        return {
            "taken_at": self.taken_at.isoformat(),
            "entities": len(self.entities),
            "by_type": counts,
            "zones": len(self.zones),
            "open_alerts": self.open_alerts,
            "events_last_hour": self.events_last_hour,
        }


def snapshot_from_api(
    *,
    taken_at: datetime,
    entities: list[dict[str, Any]],
    zones: list[dict[str, Any]],
    open_alerts: int = 0,
    events_last_hour: int = 0,
    tenant_id: str = "default",
) -> WorldSnapshot:
    """Build a snapshot from the API's JSON.

    Tolerant of missing fields on purpose. A projection that refuses to run because one entity has no
    velocity is less useful than one that treats it as stationary and says so — and the alternative is a
    what-if that works in tests and fails on real data.
    """
    sim_entities: list[SimEntity] = []
    for row in entities:
        state = row.get("state") or {}
        geo = state.get("geo") or {}
        if geo.get("lat") is None or geo.get("lon") is None:
            # No position, no place in a spatial projection. Counted as skipped by the caller rather than
            # silently dropped, because "we ignored 40 entities" changes how much the answer is worth.
            continue
        velocity = state.get("velocity") or {}
        speed = math.hypot(float(velocity.get("east", 0.0)), float(velocity.get("north", 0.0)))
        attributes = row.get("attributes") or {}
        battery = attributes.get("battery_pct")
        sim_entities.append(
            SimEntity(
                entity_id=str(row.get("entity_id", "")),
                type=str(row.get("type", "unknown")),
                label=row.get("label"),
                lat=float(geo["lat"]),
                lon=float(geo["lon"]),
                zone_id=state.get("zone_id"),
                speed_mps=speed,
                battery_pct=float(battery) if battery is not None else None,
                attributes=attributes,
            )
        )

    sim_zones: list[SimZone] = []
    for row in zones:
        geometry = row.get("geometry") or {}
        ring: tuple[tuple[float, float], ...] = ()
        coordinates = geometry.get("coordinates")
        if geometry.get("type") == "Polygon" and coordinates:
            ring = tuple((float(point[0]), float(point[1])) for point in coordinates[0])
        centre_lat, centre_lon = _centroid(ring)
        sim_zones.append(
            SimZone(
                zone_id=str(row.get("zone_id", "")),
                name=str(row.get("name") or row.get("zone_id") or ""),
                kind=str(row.get("kind") or "area"),
                lat=centre_lat,
                lon=centre_lon,
                restricted=bool(row.get("restricted")),
                capacity=row.get("capacity"),
                polygon=ring,
            )
        )

    return WorldSnapshot(
        taken_at=taken_at,
        entities=tuple(sim_entities),
        zones=tuple(sim_zones),
        open_alerts=open_alerts,
        events_last_hour=events_last_hour,
        tenant_id=tenant_id,
    )


def _centroid(ring: tuple[tuple[float, float], ...]) -> tuple[float, float]:
    """Mean of the vertices. Not the area centroid, and that is fine here.

    The area centroid is the correct thing for an irregular polygon and needs the shoelace formula; the
    vertex mean is a few metres off on a rectangular dock and is only ever used as "roughly where this zone
    is". Precision that nothing consumes is a cost with no benefit.
    """
    if not ring:
        return 0.0, 0.0
    return (
        sum(point[1] for point in ring) / len(ring),
        sum(point[0] for point in ring) / len(ring),
    )


__all__ = [
    "METRES_PER_DEGREE",
    "SimEntity",
    "SimZone",
    "WorldSnapshot",
    "snapshot_from_api",
]
