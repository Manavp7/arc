"""Image-to-ground projection: turning a camera detection into a position on the site.

A camera track is a box in image space. Fusion needs a position on the ground, and getting there
requires knowing where the camera is and where it points. That is *calibration*, and it is the
unglamorous part of every real deployment.

The model here is the standard one for a fixed camera over flat ground:

* **bearing** comes from the box's horizontal position. A detection at the centre of frame lies along
  the optical axis; one at the edge lies at half the field of view off-axis. Linear in between, which
  is a small-angle approximation that is fine for a 60-90 degree lens.
* **range** comes from the box's bottom edge under a flat-ground assumption, by inverting the pinhole
  projection exactly: a ground point at distance ``d`` seen from height ``h`` has depression angle
  ``atan(h/d)``, which maps linearly to an image row given the tilt and vertical field of view. Near
  the horizon the inverse is ill-conditioned, so the uncertainty it reports grows accordingly and
  fixes beyond the camera's rated range are refused outright.

An earlier version used a hand-tuned curve here and a *different* hand-tuned curve in the simulator's
forward projection. They disagreed by 10 to 28 metres — far outside any sensible association gate, and
the reason camera tracks never fused with GPS tracks. Both sides now use the same physics, which is
what calibration means.

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
    """Mounting height above the ground plane."""
    tilt_deg: float = 18.0
    """Downward tilt of the optical axis."""
    vfov_deg: float = 45.0
    """Vertical field of view."""
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
            tilt_deg=float(config.get("tilt_deg", 18.0)),
            vfov_deg=float(config.get("vfov_deg", 45.0)),
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

        # --- range from the ground contact row ------------------------------------
        # The exact inverse of the camera's projection:
        #     depression = tilt + ((row - H/2) / (H/2)) * (vfov / 2)
        #     distance   = height / tan(depression)
        depression_deg = calibration.tilt_deg + (
            (ground_y - calibration.frame_height / 2) / (calibration.frame_height / 2)
        ) * (calibration.vfov_deg / 2)
        if depression_deg <= 0.15:
            return None  # at or above the horizon: the ray never meets the ground
        range_m = calibration.height_m / math.tan(math.radians(depression_deg))
        if range_m > calibration.range_m * 1.5:
            # Beyond what this camera claims to cover. Returning a 300 m fix from a camera rated to
            # 60 m would let one badly-placed box drag an entity across the site.
            return None

        # --- uncertainty ----------------------------------------------------------
        # A pixel of vertical error subtends a fixed angle, and that angle maps to more ground
        # distance the flatter the ray, so range error grows with roughly the square of range. Taking
        # it from the derivative of h/tan(a) rather than from a fitted constant means it stays correct
        # if the mounting height or tilt ever changes.
        pixel_angle_rad = math.radians(calibration.vfov_deg / calibration.frame_height)
        depression_rad = math.radians(depression_deg)
        range_sigma = (
            abs(calibration.height_m * pixel_angle_rad / (math.sin(depression_rad) ** 2)) * 2.0
        )  # two pixels of contact-point uncertainty: detection boxes are not exact
        # Bearing error contributes across-track uncertainty, which grows linearly with range.
        bearing_sigma = range_m * math.radians(calibration.fov_deg / calibration.frame_width) * 3.0
        sigma = max(0.5, math.hypot(range_sigma, bearing_sigma))

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
