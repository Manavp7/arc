"""Geospatial and image-space primitives."""

from __future__ import annotations

import math

from pydantic import Field, model_validator

from .base import SioModel

EARTH_RADIUS_M = 6_371_008.8


class Geo(SioModel):
    """A point on Earth. WGS84 unless ``crs`` says otherwise."""

    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    alt: float | None = Field(default=None, description="Metres above the WGS84 ellipsoid")
    crs: str = Field(default="EPSG:4326")

    def distance_to(self, other: Geo) -> float:
        """Great-circle distance in metres (haversine).

        Good to ~0.5 % — ample for association gating and "within N metres" answers at site
        scale. PostGIS ``geography`` does the authoritative maths for stored queries.
        """
        lat1, lon1, lat2, lon2 = map(math.radians, (self.lat, self.lon, other.lat, other.lon))
        dlat, dlon = lat2 - lat1, lon2 - lon1
        a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))

    def bearing_to(self, other: Geo) -> float:
        """Initial bearing to ``other`` in degrees clockwise from true north."""
        lat1, lat2 = math.radians(self.lat), math.radians(other.lat)
        dlon = math.radians(other.lon - self.lon)
        y = math.sin(dlon) * math.cos(lat2)
        x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
        return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0

    def offset(self, north_m: float, east_m: float) -> Geo:
        """Return a point offset by the given metres. Local flat-earth approximation."""
        dlat = north_m / EARTH_RADIUS_M
        dlon = east_m / (EARTH_RADIUS_M * math.cos(math.radians(self.lat)))
        return Geo(
            lat=self.lat + math.degrees(dlat),
            lon=self.lon + math.degrees(dlon),
            alt=self.alt,
            crs=self.crs,
        )

    def as_geojson(self) -> dict[str, object]:
        coords: list[float] = [self.lon, self.lat]
        if self.alt is not None:
            coords.append(self.alt)
        return {"type": "Point", "coordinates": coords}


class Velocity(SioModel):
    """Ground velocity in metres per second, plus derived heading/speed helpers."""

    north: float = 0.0
    east: float = 0.0
    up: float = 0.0

    @property
    def speed_mps(self) -> float:
        return math.hypot(self.north, self.east)

    @property
    def speed_kmh(self) -> float:
        return self.speed_mps * 3.6

    @property
    def heading_deg(self) -> float:
        """Direction of travel, degrees clockwise from north."""
        return (math.degrees(math.atan2(self.east, self.north)) + 360.0) % 360.0


class BBox(SioModel):
    """Axis-aligned box in *source image pixels* (not normalised).

    Pixel space is deliberate: a bbox is only meaningful alongside the frame it came from,
    and normalising loses the resolution needed to crop for ReID/OCR later.
    """

    x1: float = Field(ge=0.0)
    y1: float = Field(ge=0.0)
    x2: float = Field(ge=0.0)
    y2: float = Field(ge=0.0)

    @model_validator(mode="after")
    def _ordered(self) -> BBox:
        if self.x2 < self.x1 or self.y2 < self.y1:
            raise ValueError(f"bbox corners out of order: ({self.x1},{self.y1})-({self.x2},{self.y2})")
        return self

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> tuple[float, float]:
        return (self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0

    @property
    def xywh(self) -> tuple[float, float, float, float]:
        return self.x1, self.y1, self.width, self.height

    def iou(self, other: BBox) -> float:
        """Intersection over union — the association metric used by the tracker."""
        ix1, iy1 = max(self.x1, other.x1), max(self.y1, other.y1)
        ix2, iy2 = min(self.x2, other.x2), min(self.y2, other.y2)
        if ix2 <= ix1 or iy2 <= iy1:
            return 0.0
        inter = (ix2 - ix1) * (iy2 - iy1)
        union = self.area + other.area - inter
        return inter / union if union > 0 else 0.0

    def expand(self, ratio: float) -> BBox:
        """Grow the box by ``ratio`` on every side (used to give ReID crops some context)."""
        dx, dy = self.width * ratio, self.height * ratio
        return BBox(
            x1=max(0.0, self.x1 - dx),
            y1=max(0.0, self.y1 - dy),
            x2=self.x2 + dx,
            y2=self.y2 + dy,
        )

    def clip(self, width: float, height: float) -> BBox:
        return BBox(
            x1=min(max(0.0, self.x1), width),
            y1=min(max(0.0, self.y1), height),
            x2=min(max(0.0, self.x2), width),
            y2=min(max(0.0, self.y2), height),
        )
