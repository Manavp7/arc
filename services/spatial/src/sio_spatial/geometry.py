"""Zone geometry and H3 indexing.

Two jobs that both need to be fast and to agree with PostGIS.

**Zone membership on the hot path.** Every fused entity update needs to know which zones contain it.
A PostGIS round trip per update would be a query per entity per second, so zone polygons are loaded
once into shapely with an R-tree and tested locally. PostGIS stays the source of truth for ad-hoc
queries, and an infra test asserts the two agree on sample points — because two implementations of
"is this point inside that polygon" that quietly disagree is a bug that shows up as an event storm at
3 a.m. rather than as a failing assertion.

**H3 cells for aggregation.** Counting entities per zone answers "how busy is the dock"; counting per
H3 cell answers "where exactly in the yard do people cluster", without pre-defining a zone for every
question. H3 is used rather than a raw grid because its cells have uniform area and every cell has six
equidistant neighbours, so "expand the search by one ring" is meaningful in a way that a lat/lon grid
cannot be at any latitude.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import h3
from shapely.geometry import Point, Polygon, shape
from shapely.strtree import STRtree

from sio_core import get_logger
from sio_schemas import Geo

log = get_logger("sio.spatial.geometry")

# Resolution 12: about 307 m^2 per cell, roughly 19 m across.
#
# Chosen to be comparable to the objects being aggregated — a box truck is some 15 m long, so a cell
# holds about one vehicle. Coarser cells (res 9 at 105,000 m^2) would put the whole dock apron in one
# bucket and answer nothing; finer cells (res 14 at 6 m^2) would scatter a single truck across a dozen
# cells and make every count noise. A yard of 400 x 300 m is about 390 cells at this resolution, which
# is a heatmap a human can read.
DEFAULT_H3_RESOLUTION = 12

EARTH_RADIUS_M = 6_371_008.8


def cell_for(geo: Geo, resolution: int = DEFAULT_H3_RESOLUTION) -> str:
    """H3 cell containing a position."""
    return h3.latlng_to_cell(geo.lat, geo.lon, resolution)


def cell_centre(cell: str) -> Geo:
    lat, lon = h3.cell_to_latlng(cell)
    return Geo(lat=lat, lon=lon)


def cells_within(geo: Geo, radius_m: float, resolution: int = DEFAULT_H3_RESOLUTION) -> list[str]:
    """Every cell whose centre lies within ``radius_m``.

    Used to turn a radius query into a set of index lookups. The ring count is derived from the
    resolution's own edge length rather than hard-coded, so changing the resolution does not silently
    change what "within 500 m" means.
    """
    edge_m = h3.average_hexagon_edge_length(resolution, unit="m")
    rings = max(1, math.ceil(radius_m / (edge_m * 1.5)))
    origin = h3.latlng_to_cell(geo.lat, geo.lon, resolution)
    candidates = h3.grid_disk(origin, rings)
    return [cell for cell in candidates if haversine_m(geo, cell_centre(cell)) <= radius_m]


def haversine_m(a: Geo, b: Geo) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a.lat, a.lon, b.lat, b.lon))
    delta_lat, delta_lon = lat2 - lat1, lon2 - lon1
    inner = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(inner))


def metres_to_degrees(metres: float, latitude: float) -> tuple[float, float]:
    """Convert a distance to degrees of latitude and longitude at a given latitude.

    Needed because shapely works in the coordinate units it is given, and those are degrees here. A
    single "degrees per metre" constant would be wrong in longitude everywhere except the equator.
    """
    lat_degrees = metres / EARTH_RADIUS_M * 180.0 / math.pi
    lon_degrees = lat_degrees / max(0.01, math.cos(math.radians(latitude)))
    return lat_degrees, lon_degrees


@dataclass(frozen=True)
class ZoneShape:
    """One zone with its geometry ready for point-in-polygon tests."""

    zone_id: str
    name: str
    kind: str
    restricted: bool
    polygon: Polygon
    capacity: int | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def contains(self, geo: Geo) -> bool:
        return self.polygon.contains(Point(geo.lon, geo.lat))

    def distance_to_boundary_m(self, geo: Geo) -> float:
        """Signed-magnitude distance from a point to the zone edge, in metres.

        Positive inside, negative outside. Used for hysteresis: a GPS fix two metres inside a boundary
        is not a confident entry, and treating it as one is what produces event storms.
        """
        point = Point(geo.lon, geo.lat)
        # Distance in degrees, converted at this latitude. Good enough at site scale and far cheaper
        # than reprojecting the polygon per query.
        degrees = point.distance(self.polygon.exterior)
        lat_degrees, _ = metres_to_degrees(1.0, geo.lat)
        metres = degrees / lat_degrees if lat_degrees else 0.0
        return metres if self.polygon.contains(point) else -metres

    @property
    def centroid(self) -> Geo:
        centre = self.polygon.centroid
        return Geo(lat=centre.y, lon=centre.x)


class ZoneIndex:
    """An in-memory, R-tree-backed index of zone polygons."""

    def __init__(self, zones: list[ZoneShape] | None = None) -> None:
        self.zones: list[ZoneShape] = list(zones or [])
        self._tree: STRtree | None = None
        self._rebuild()

    def _rebuild(self) -> None:
        self._tree = STRtree([zone.polygon for zone in self.zones]) if self.zones else None

    def replace(self, zones: list[ZoneShape]) -> None:
        """Swap in a fresh set of zones. Zones change rarely, but they do change."""
        self.zones = list(zones)
        self._rebuild()

    def __len__(self) -> int:
        return len(self.zones)

    def get(self, zone_id: str) -> ZoneShape | None:
        return next((zone for zone in self.zones if zone.zone_id == zone_id), None)

    def zones_containing(self, geo: Geo) -> list[ZoneShape]:
        """Every zone containing a point, innermost first.

        Zones nest — a restricted cage sits inside a yard — so this returns all of them ordered by
        area. Returning only one would make "is this person in a restricted area?" depend on
        insertion order, and the smallest enclosing zone is the most specific answer.
        """
        if self._tree is None:
            return []
        point = Point(geo.lon, geo.lat)
        candidates = [self.zones[index] for index in self._tree.query(point)]
        matched = [zone for zone in candidates if zone.polygon.contains(point)]
        return sorted(matched, key=lambda zone: zone.polygon.area)

    def innermost(self, geo: Geo) -> ZoneShape | None:
        containing = self.zones_containing(geo)
        return containing[0] if containing else None

    def nearest(self, geo: Geo, *, kind: str | None = None) -> tuple[ZoneShape, float] | None:
        """The closest zone and its distance in metres, optionally filtered by kind."""
        candidates = [zone for zone in self.zones if kind is None or zone.kind == kind]
        if not candidates:
            return None
        best = min(candidates, key=lambda zone: haversine_m(geo, zone.centroid))
        return best, haversine_m(geo, best.centroid)


def zone_shape_from_row(row: dict[str, Any]) -> ZoneShape | None:
    """Build a ZoneShape from a ``zones`` row whose geometry arrived as GeoJSON."""
    geometry = row.get("geojson")
    if not geometry:
        return None
    polygon = shape(geometry if isinstance(geometry, dict) else __import__("json").loads(geometry))
    if not isinstance(polygon, Polygon):
        return None
    return ZoneShape(
        zone_id=str(row["zone_id"]),
        name=str(row.get("name") or row["zone_id"]),
        kind=str(row.get("kind") or "area"),
        restricted=bool(row.get("restricted")),
        polygon=polygon,
        capacity=row.get("capacity"),
        attributes=dict(row.get("attributes") or {}),
    )


@dataclass(frozen=True)
class CameraFootprint:
    """A camera's field of view as a ground polygon, for coverage and blind-spot queries."""

    source_id: str
    geo: Geo
    bearing_deg: float
    fov_deg: float
    range_m: float
    polygon: Polygon

    @classmethod
    def build(
        cls,
        source_id: str,
        geo: Geo,
        bearing_deg: float,
        fov_deg: float,
        range_m: float,
        *,
        segments: int = 12,
    ) -> CameraFootprint:
        """Approximate the FOV as a circular sector.

        A sector rather than a triangle: a triangle underestimates coverage at the far edge by the
        difference between a chord and an arc, which at 70 degrees is about 8 per cent of the range —
        enough to fabricate a blind spot that does not exist.
        """
        lat_degrees, lon_degrees = metres_to_degrees(range_m, geo.lat)
        points = [(geo.lon, geo.lat)]
        start = bearing_deg - fov_deg / 2
        for index in range(segments + 1):
            bearing = math.radians(start + fov_deg * index / segments)
            points.append(
                (
                    geo.lon + math.sin(bearing) * lon_degrees,
                    geo.lat + math.cos(bearing) * lat_degrees,
                )
            )
        return cls(
            source_id=source_id,
            geo=geo,
            bearing_deg=bearing_deg,
            fov_deg=fov_deg,
            range_m=range_m,
            polygon=Polygon(points),
        )

    def covers(self, geo: Geo) -> bool:
        return self.polygon.contains(Point(geo.lon, geo.lat))
