"""The simulated site: a logistics yard.

PRD open question Q2 resolved to a distribution centre, because it exercises the use cases
directly: trucks arriving and dwelling (UC1), a fire at a dock triggering a playbook (UC2),
"which camera last saw this truck" (UC3), yard congestion forecasting (UC4), and replaying an
incident (UC5).

The geometry is defined in **local metres** and projected to WGS84 around an origin. Authoring in
metres means the layout is readable and editable ("dock 3 is 40 m east of dock 2") instead of a
wall of six-decimal coordinates, and the yard can be relocated anywhere on Earth by changing one
constant.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sio_schemas import EntityType, Geo

# A yard on the eastern edge of San Francisco. Arbitrary but real coordinates: real ones mean
# PostGIS distances, H3 cells and any future basemap all behave as they would in production.
ORIGIN = Geo(lat=37.7749, lon=-122.4194)

EARTH_RADIUS_M = 6_371_008.8

ZoneKind = Literal["gate", "dock", "lane", "restricted", "area", "parking", "building"]


def to_geo(east_m: float, north_m: float, origin: Geo = ORIGIN) -> Geo:
    """Project local metres (east, north) to WGS84 around ``origin``."""
    dlat = north_m / EARTH_RADIUS_M
    dlon = east_m / (EARTH_RADIUS_M * math.cos(math.radians(origin.lat)))
    return Geo(lat=origin.lat + math.degrees(dlat), lon=origin.lon + math.degrees(dlon))


def to_local(geo: Geo, origin: Geo = ORIGIN) -> tuple[float, float]:
    """Inverse of :func:`to_geo` — used by the simulator's motion model."""
    north = math.radians(geo.lat - origin.lat) * EARTH_RADIUS_M
    east = math.radians(geo.lon - origin.lon) * EARTH_RADIUS_M * math.cos(math.radians(origin.lat))
    return east, north


@dataclass(frozen=True)
class Point:
    """A named waypoint, in local metres."""

    name: str
    east: float
    north: float

    @property
    def geo(self) -> Geo:
        return to_geo(self.east, self.north)

    def distance_to(self, other: Point) -> float:
        return math.hypot(other.east - self.east, other.north - self.north)


@dataclass(frozen=True)
class Zone:
    """A polygonal area of the site."""

    zone_id: str
    name: str
    kind: ZoneKind
    corners: tuple[tuple[float, float], ...]
    restricted: bool = False
    capacity: int | None = None

    def contains(self, east: float, north: float) -> bool:
        """Ray-casting point-in-polygon.

        Hand-rolled rather than pulled from Shapely: this runs for every agent on every tick, and
        the authoritative spatial predicates live in PostGIS anyway (the spatial engine, Phase 3).
        """
        inside = False
        count = len(self.corners)
        for index in range(count):
            x1, y1 = self.corners[index]
            x2, y2 = self.corners[(index + 1) % count]
            if (y1 > north) != (y2 > north):
                x_at = x1 + (north - y1) * (x2 - x1) / (y2 - y1)
                if east < x_at:
                    inside = not inside
        return inside

    @property
    def centroid(self) -> tuple[float, float]:
        east = sum(corner[0] for corner in self.corners) / len(self.corners)
        north = sum(corner[1] for corner in self.corners) / len(self.corners)
        return east, north

    def as_geojson_geometry(self) -> dict[str, Any]:
        ring = [[to_geo(e, n).lon, to_geo(e, n).lat] for e, n in self.corners]
        ring.append(ring[0])  # GeoJSON polygons must close
        return {"type": "Polygon", "coordinates": [ring]}


@dataclass(frozen=True)
class Camera:
    """A fixed camera with a real pose: position, bearing, field of view, height and tilt.

    The FOV decides which agents a camera can see (so the simulator only emits frames containing
    something), answers "which cameras cover Gate B", and its complement is the blind-spot query
    (PRD M6).

    ``height_m``, ``tilt_deg`` and ``vfov_deg`` exist so that image position and ground distance are
    related by an actual pinhole projection rather than by a curve that looks about right. The
    simulator projects ground positions into the image with this model and fusion inverts it with the
    same one — which is what calibration *is*. Two different approximations left the fused position
    10-28 m from the truth, well outside any sane association gate.
    """

    source_id: str
    east: float
    north: float
    bearing_deg: float
    fov_deg: float = 70.0
    range_m: float = 60.0
    label: str = ""
    covers: tuple[str, ...] = ()
    height_m: float = 6.0
    """Mounting height above the ground plane."""
    tilt_deg: float = 18.0
    """Downward tilt of the optical axis. With a 6 m mount this puts the axis on the ground at ~18 m."""
    vfov_deg: float = 45.0
    """Vertical field of view."""

    @property
    def geo(self) -> Geo:
        return to_geo(self.east, self.north)

    def sees(self, east: float, north: float) -> bool:
        distance = math.hypot(east - self.east, north - self.north)
        if distance > self.range_m or distance < 0.5:
            return False
        bearing = (math.degrees(math.atan2(east - self.east, north - self.north)) + 360) % 360
        delta = abs((bearing - self.bearing_deg + 180) % 360 - 180)
        return delta <= self.fov_deg / 2

    def fov_polygon(self, segments: int = 8) -> dict[str, Any]:
        """The FOV as a GeoJSON wedge, for the map and for PostGIS coverage queries."""
        half = self.fov_deg / 2
        ring = [[self.geo.lon, self.geo.lat]]
        for index in range(segments + 1):
            angle = math.radians(self.bearing_deg - half + (self.fov_deg * index / segments))
            east = self.east + math.sin(angle) * self.range_m
            north = self.north + math.cos(angle) * self.range_m
            point = to_geo(east, north)
            ring.append([point.lon, point.lat])
        ring.append([self.geo.lon, self.geo.lat])
        return {"type": "Polygon", "coordinates": [ring]}


@dataclass(frozen=True)
class Sensor:
    """A non-camera fixed sensor (temperature, power, RFID reader, door contact)."""

    source_id: str
    east: float
    north: float
    metric: str
    unit: str
    label: str = ""
    zone_id: str | None = None
    baseline: float = 20.0
    noise: float = 0.4

    @property
    def geo(self) -> Geo:
        return to_geo(self.east, self.north)


@dataclass
class Site:
    """The whole facility: zones, cameras, sensors and the road graph agents drive on."""

    name: str
    origin: Geo
    zones: list[Zone] = field(default_factory=list)
    cameras: list[Camera] = field(default_factory=list)
    sensors: list[Sensor] = field(default_factory=list)
    waypoints: dict[str, Point] = field(default_factory=dict)
    routes: dict[str, list[str]] = field(default_factory=dict)

    # -------------------------------------------------------------------- lookup
    def zone_at(self, east: float, north: float) -> Zone | None:
        """Innermost zone containing the point.

        Smallest-area-first, because a dock sits inside the yard which sits inside the perimeter,
        and "in dock 3" is the useful answer, not "in the site".
        """
        matches = [zone for zone in self.zones if zone.contains(east, north)]
        if not matches:
            return None
        return min(matches, key=lambda zone: _polygon_area(zone.corners))

    def zone(self, zone_id: str) -> Zone | None:
        return next((zone for zone in self.zones if zone.zone_id == zone_id), None)

    def waypoint(self, name: str) -> Point:
        point = self.waypoints.get(name)
        if point is None:
            raise KeyError(f"unknown waypoint {name!r}; known: {sorted(self.waypoints)}")
        return point

    def route(self, name: str) -> list[Point]:
        return [self.waypoint(step) for step in self.routes[name]]

    def cameras_seeing(self, east: float, north: float) -> list[Camera]:
        return [camera for camera in self.cameras if camera.sees(east, north)]

    def dock_ids(self) -> list[str]:
        return [zone.zone_id for zone in self.zones if zone.kind == "dock"]

    # ------------------------------------------------------------------- exports
    def as_geojson(self) -> dict[str, Any]:
        """The whole site as a FeatureCollection — what the UI draws and `just seed` loads."""
        features: list[dict[str, Any]] = []
        for zone in self.zones:
            features.append(
                {
                    "type": "Feature",
                    "geometry": zone.as_geojson_geometry(),
                    "properties": {
                        "kind": "zone",
                        "zone_id": zone.zone_id,
                        "name": zone.name,
                        "zone_kind": zone.kind,
                        "restricted": zone.restricted,
                        "capacity": zone.capacity,
                    },
                }
            )
        for camera in self.cameras:
            features.append(
                {
                    "type": "Feature",
                    "geometry": camera.geo.as_geojson(),
                    "properties": {
                        "kind": "camera",
                        "source_id": camera.source_id,
                        "label": camera.label,
                        "bearing_deg": camera.bearing_deg,
                        "range_m": camera.range_m,
                        "covers": list(camera.covers),
                        "entity_type": str(EntityType.CAMERA),
                    },
                }
            )
            features.append(
                {
                    "type": "Feature",
                    "geometry": camera.fov_polygon(),
                    "properties": {
                        "kind": "camera_fov",
                        "source_id": camera.source_id,
                        "label": f"{camera.label} field of view",
                    },
                }
            )
        for sensor in self.sensors:
            features.append(
                {
                    "type": "Feature",
                    "geometry": sensor.geo.as_geojson(),
                    "properties": {
                        "kind": "sensor",
                        "source_id": sensor.source_id,
                        "label": sensor.label,
                        "metric": sensor.metric,
                        "unit": sensor.unit,
                        "zone_id": sensor.zone_id,
                        "entity_type": str(EntityType.SENSOR),
                    },
                }
            )
        for name, point in self.waypoints.items():
            features.append(
                {
                    "type": "Feature",
                    "geometry": point.geo.as_geojson(),
                    "properties": {"kind": "waypoint", "name": name},
                }
            )
        return {
            "type": "FeatureCollection",
            "properties": {
                "name": self.name,
                "origin": {"lat": self.origin.lat, "lon": self.origin.lon},
                "routes": self.routes,
            },
            "features": features,
        }

    def write_geojson(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_geojson(), indent=2) + "\n")
        return path


def _polygon_area(corners: tuple[tuple[float, float], ...]) -> float:
    """Shoelace area, used only to rank nested zones."""
    total = 0.0
    for index in range(len(corners)):
        x1, y1 = corners[index]
        x2, y2 = corners[(index + 1) % len(corners)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2


def _rect(east: float, north: float, width: float, depth: float) -> tuple[tuple[float, float], ...]:
    """Axis-aligned rectangle from its south-west corner."""
    return (
        (east, north),
        (east + width, north),
        (east + width, north + depth),
        (east, north + depth),
    )


def default_yard() -> Site:
    """The demo site: a 420 m x 260 m distribution-centre yard.

    Layout, looking north:

        ┌──────────────────── warehouse (docks 1-6 along its south face) ─────────┐
        │  D1   D2   D3   D4   D5   D6                                            │
        └─────────────────────────────────────────────────────────────────────────┘
             ↑ dock apron
        ════════════════ north lane ════════════════════════════════════
        Gate A →                                         staging/parking      → Gate B
        ════════════════ south lane ════════════════════════════════════
                                             fuel store (restricted)
    """
    site = Site(name="Northgate Distribution Centre", origin=ORIGIN)

    # ---- outer boundary and driving surface --------------------------------
    site.zones.append(Zone("perimeter", "Site perimeter", "area", _rect(-10, -10, 440, 280)))
    site.zones.append(Zone("yard", "Yard", "area", _rect(10, 20, 400, 180)))
    site.zones.append(Zone("lane_north", "North lane", "lane", _rect(20, 150, 380, 14)))
    site.zones.append(Zone("lane_south", "South lane", "lane", _rect(20, 60, 380, 14)))
    site.zones.append(Zone("apron", "Dock apron", "area", _rect(40, 168, 320, 30)))

    # ---- gates -------------------------------------------------------------
    site.zones.append(Zone("gate_a", "Gate A (entry)", "gate", _rect(4, 58, 18, 18), capacity=1))
    site.zones.append(Zone("gate_b", "Gate B (exit)", "gate", _rect(398, 58, 18, 18), capacity=1))

    # ---- warehouse and dock doors -----------------------------------------
    site.zones.append(Zone("warehouse", "Warehouse", "building", _rect(40, 198, 320, 50)))
    for index in range(6):
        east = 56 + index * 52
        site.zones.append(
            Zone(
                f"dock_{index + 1}",
                f"Dock {index + 1}",
                "dock",
                _rect(east, 176, 22, 22),
                capacity=1,
            )
        )

    # ---- staging, parking, restricted --------------------------------------
    site.zones.append(
        Zone("staging", "Staging area", "parking", _rect(300, 88, 90, 50), capacity=8)
    )
    site.zones.append(
        Zone("fuel_store", "Fuel store", "restricted", _rect(210, 26, 46, 26), restricted=True)
    )
    site.zones.append(Zone("office", "Office", "building", _rect(20, 210, 40, 30)))

    # ---- waypoints ---------------------------------------------------------
    waypoints = {
        "gate_a_approach": (-4, 67),
        "gate_a": (13, 67),
        "gate_a_inner": (34, 67),
        "south_west": (60, 67),
        "south_mid": (200, 67),
        "south_east": (340, 67),
        "gate_b_inner": (386, 67),
        "gate_b": (407, 67),
        "gate_b_exit": (426, 67),
        "west_link": (34, 110),
        "east_link": (386, 110),
        "north_west": (60, 157),
        "north_mid": (200, 157),
        "north_east": (340, 157),
        "staging_in": (312, 100),
        "staging_bay": (340, 112),
        "fuel_store": (233, 39),
        "office_door": (40, 208),
        "yard_centre": (200, 110),
    }
    for index in range(6):
        waypoints[f"dock_{index + 1}_approach"] = (67 + index * 52, 160)
        waypoints[f"dock_{index + 1}_bay"] = (67 + index * 52, 183)
        # Where the forklift works: beside the bay, not in it. Two entities at the same coordinate
        # render as one dot with two overprinted labels.
        waypoints[f"dock_{index + 1}_side"] = (67 + index * 52 + 15, 180)
    site.waypoints = {name: Point(name, e, n) for name, (e, n) in waypoints.items()}

    # ---- routes ------------------------------------------------------------
    site.routes = {
        "arrive": [
            "gate_a_approach",
            "gate_a",
            "gate_a_inner",
            "south_west",
            "west_link",
            "north_west",
        ],
        "depart": [
            "north_east",
            "east_link",
            "south_east",
            "gate_b_inner",
            "gate_b",
            "gate_b_exit",
        ],
        "to_staging": ["south_mid", "staging_in", "staging_bay"],
        "patrol": [
            "gate_a_inner",
            "south_mid",
            "south_east",
            "east_link",
            "north_east",
            "north_mid",
            "north_west",
            "west_link",
        ],
        "forklift_loop": ["dock_2_bay", "dock_2_approach", "dock_4_approach", "dock_4_bay"],
        "worker_walk": ["office_door", "north_west", "north_mid", "dock_3_approach"],
    }
    for index in range(6):
        site.routes[f"to_dock_{index + 1}"] = [
            "north_west",
            f"dock_{index + 1}_approach",
            f"dock_{index + 1}_bay",
        ]

    # ---- cameras -----------------------------------------------------------
    site.cameras = [
        Camera("cam-gate-a", 24, 80, 180, 75, 55, "Gate A", ("gate_a", "lane_south")),
        Camera("cam-gate-b", 396, 80, 180, 75, 55, "Gate B", ("gate_b", "lane_south")),
        Camera("cam-dock-1-2", 82, 168, 0, 80, 45, "Docks 1-2", ("dock_1", "dock_2", "apron")),
        Camera("cam-dock-3-4", 186, 168, 0, 80, 45, "Docks 3-4", ("dock_3", "dock_4", "apron")),
        Camera("cam-dock-5-6", 290, 168, 0, 80, 45, "Docks 5-6", ("dock_5", "dock_6", "apron")),
        Camera("cam-yard-west", 60, 120, 90, 90, 70, "Yard west", ("yard", "lane_south")),
        Camera("cam-yard-east", 340, 120, 270, 90, 70, "Yard east", ("yard", "staging")),
        Camera("cam-fuel", 233, 60, 180, 60, 40, "Fuel store", ("fuel_store",)),
    ]

    # ---- fixed sensors -----------------------------------------------------
    site.sensors = [
        Sensor(
            "iot-temp-dock-3",
            190,
            180,
            "temperature_c",
            "°C",
            "Dock 3 temperature",
            "dock_3",
            21.0,
            0.5,
        ),
        Sensor(
            "iot-temp-dock-5",
            294,
            180,
            "temperature_c",
            "°C",
            "Dock 5 temperature",
            "dock_5",
            20.5,
            0.5,
        ),
        Sensor(
            "iot-temp-fuel",
            233,
            39,
            "temperature_c",
            "°C",
            "Fuel store temperature",
            "fuel_store",
            19.0,
            0.4,
        ),
        Sensor(
            "iot-humidity-wh",
            200,
            220,
            "humidity_pct",
            "%",
            "Warehouse humidity",
            "warehouse",
            48.0,
            2.0,
        ),
        Sensor(
            "iot-power-main", 30, 215, "power_kw", "kW", "Main switchboard", "office", 240.0, 12.0
        ),
        Sensor(
            "iot-rfid-gate-a",
            13,
            67,
            "rfid_read",
            "count",
            "Gate A RFID reader",
            "gate_a",
            0.0,
            0.0,
        ),
        Sensor(
            "iot-rfid-gate-b",
            407,
            67,
            "rfid_read",
            "count",
            "Gate B RFID reader",
            "gate_b",
            0.0,
            0.0,
        ),
        Sensor(
            "iot-door-fuel", 210, 39, "door_open", "bool", "Fuel store door", "fuel_store", 0.0, 0.0
        ),
    ]

    return site


def load_site(path: Path | None = None) -> Site:
    """Load the site.

    Currently always the built-in yard: the GeoJSON export is a *view* of the site for the UI and
    for PostGIS, not the source of truth, because behaviour (routes, dock capacity, sensor
    baselines) does not round-trip through GeoJSON properties cleanly. Real-site loading arrives
    with the connector work in Phase 7.
    """
    return default_yard()
