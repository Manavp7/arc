"""Image-to-ground projection: turning a camera detection into a position on the site.

A camera track is a box in image space. Fusion needs a position on the ground, and getting there
requires knowing where the camera is and where it points. That is *calibration*, and it is the
unglamorous part of every real deployment.

The model here is the standard one for a fixed camera over flat ground:

* **bearing** comes from the box's horizontal position. A detection at the centre of frame lies along
  the optical axis; one at the edge lies at half the field of view off-axis. Linear in between, which
  is a small-angle approximation that is fine for a 60-90 degree lens.
* **range** comes from the box's vertical position, under a flat-ground assumption. The bottom of the
  box is where the object meets the ground, and for a camera at a known height and tilt that maps
  monotonically to distance. Near the horizon the mapping becomes ill-conditioned, so range is capped
  and the uncertainty grows with distance.

Both assumptions are stated rather than hidden, because they decide when the output can be trusted:
flat ground within a yard is reasonable; a sloped approach road is not. `position_sigma_m` reports the
resulting uncertainty so fusion can weight a camera fix against a GPS fix rather than treating them as
equally good.

Real deployments replace this with a surveyed homography (four ground points) or a calibration tool
such as DeepStream's AutoMagicCalib. The `GroundProjector` interface is the seam for that.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from sio_core import get_logger
from sio_schemas import BBox, Geo

log = get_logger("sio.fusion.projection")

EARTH_RADIUS_M = 6_371_008.8


@dataclass(frozen=True)
class CameraCalibration:
    """What a camera needs to place a detection on the ground."""

    source_id: str
    geo: Geo
    bearing_deg: float
    fov_deg: float = 70.0
    range_m: float = 60.0
    height_m: float = 6.0
    """Mounting height. Affects the vertical-position-to-range mapping."""
    frame_width: int = 1280
    frame_height: int = 720
    zone_id: str | None = None

    @classmethod
    def from_source_row(cls, row: dict[str, Any]) -> CameraCalibration | None:
        """Build from a ``sources`` table row, or None if it is not a usable camera.

        Calibration lives in the database rather than in code: the site is *data*, and a fusion
        service that imported the simulator's site model to learn camera poses would be unable to
        work on a real site.
        """
        config = row.get("config") or {}
        latitude, longitude = row.get("lat"), row.get("lon")
        if latitude is None or longitude is None:
            return None
        return cls(
            source_id=str(row["source_id"]),
            geo=Geo(lat=float(latitude), lon=float(longitude)),
            bearing_deg=float(config.get("bearing_deg", 0.0)),
            fov_deg=float(config.get("fov_deg", 70.0)),
            range_m=float(config.get("range_m", 60.0)),
            height_m=float(config.get("height_m", 6.0)),
            zone_id=row.get("zone_id"),
        )


@dataclass(frozen=True)
class GroundFix:
    """A projected position with its uncertainty."""

    geo: Geo
    range_m: float
    bearing_deg: float
    position_sigma_m: float
    """One-sigma positional uncertainty. Grows with range, because that is where the flat-ground
    assumption and the pixel quantisation both bite hardest."""

    @property
    def confidence(self) -> float:
        """A rough confidence for provenance weighting: 1 at arm's length, falling with sigma."""
        return float(max(0.05, min(0.95, 6.0 / (6.0 + self.position_sigma_m))))


class GroundProjector:
    """Projects image-space boxes onto the ground for one camera."""

    def __init__(self, calibration: CameraCalibration) -> None:
        self.calibration = calibration

    def project(self, bbox: BBox) -> GroundFix | None:
        """Project a detection box to a ground position.

        Uses the box's **bottom edge** as the ground contact point, which is the only part of a
        bounding box that reliably touches the ground: the top depends on the object's height, and the
        centre depends on both.
        """
        calibration = self.calibration
        centre_x = (bbox.x1 + bbox.x2) / 2.0
        ground_y = bbox.y2

        # --- bearing from horizontal position -------------------------------------
        half_fov = calibration.fov_deg / 2.0
        offset = (
            (centre_x - calibration.frame_width / 2.0) / (calibration.frame_width / 2.0)
        ) * half_fov
        bearing = (calibration.bearing_deg + offset) % 360.0

        # --- range from vertical position -----------------------------------------
        # Normalised height down the frame: 0 at the horizon, 1 at the bottom edge. The horizon sits
        # at a third of the frame in this camera model (matching the simulator's renderer), and
        # anything above it is sky — no ground intersection exists.
        horizon = calibration.frame_height * 0.33
        if ground_y <= horizon + 1:
            return None
        depth = (ground_y - horizon) / (calibration.frame_height - horizon)
        # Inverse-perspective: the ground distance falls as the contact point moves down the frame.
        # Clamped to the camera's stated range, beyond which its own FOV polygon says it cannot see.
        range_m = min(calibration.range_m, calibration.height_m / max(0.02, depth) * 2.2)

        # --- uncertainty ----------------------------------------------------------
        # One pixel of vertical error maps to more ground distance the further away the object is, so
        # sigma grows roughly with the square of range. The constant is chosen so a 20 m detection has
        # about 2 m of positional uncertainty, which matches how well this class of estimate does in
        # practice against a GPS fix.
        sigma = 0.4 + (range_m**2) * 0.005

        geo = offset_geo(calibration.geo, bearing_deg=bearing, distance_m=range_m)
        return GroundFix(
            geo=geo,
            range_m=round(range_m, 2),
            bearing_deg=round(bearing, 1),
            position_sigma_m=round(sigma, 2),
        )

    def sees(self, geo: Geo) -> bool:
        """Could this camera see a point on the ground? Used to sanity-check an association."""
        calibration = self.calibration
        distance = haversine_m(calibration.geo, geo)
        if distance > calibration.range_m * 1.2:
            return False
        bearing = bearing_deg(calibration.geo, geo)
        delta = abs((bearing - calibration.bearing_deg + 180) % 360 - 180)
        return delta <= calibration.fov_deg / 2 + 5


def offset_geo(origin: Geo, *, bearing_deg: float, distance_m: float) -> Geo:
    """Move ``distance_m`` from ``origin`` along ``bearing_deg``. Flat-earth, exact enough at site scale."""
    radians = math.radians(bearing_deg)
    north = math.cos(radians) * distance_m
    east = math.sin(radians) * distance_m
    delta_lat = north / EARTH_RADIUS_M
    delta_lon = east / (EARTH_RADIUS_M * math.cos(math.radians(origin.lat)))
    return Geo(lat=origin.lat + math.degrees(delta_lat), lon=origin.lon + math.degrees(delta_lon))


def haversine_m(a: Geo, b: Geo) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a.lat, a.lon, b.lat, b.lon))
    delta_lat, delta_lon = lat2 - lat1, lon2 - lon1
    inner = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(inner))


def bearing_deg(origin: Geo, target: Geo) -> float:
    lat1, lat2 = math.radians(origin.lat), math.radians(target.lat)
    delta_lon = math.radians(target.lon - origin.lon)
    y = math.sin(delta_lon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def to_local_metres(geo: Geo, origin: Geo) -> tuple[float, float]:
    """Project to a local east/north frame in metres.

    Fusion runs its filter in metres, not degrees. A Kalman filter over latitude and longitude has a
    covariance whose units differ per axis and vary with latitude, which makes every gate and every
    process-noise term wrong in a way that is tedious to see. Locally-flat ENU removes the problem
    exactly at site scale.
    """
    north = math.radians(geo.lat - origin.lat) * EARTH_RADIUS_M
    east = math.radians(geo.lon - origin.lon) * EARTH_RADIUS_M * math.cos(math.radians(origin.lat))
    return east, north


def from_local_metres(east: float, north: float, origin: Geo) -> Geo:
    delta_lat = north / EARTH_RADIUS_M
    delta_lon = east / (EARTH_RADIUS_M * math.cos(math.radians(origin.lat)))
    return Geo(lat=origin.lat + math.degrees(delta_lat), lon=origin.lon + math.degrees(delta_lon))
