"""SIO spatial reasoning (PRD M6)."""

from .geometry import (
    DEFAULT_H3_RESOLUTION,
    CameraFootprint,
    ZoneIndex,
    ZoneShape,
    cell_for,
    cells_within,
    haversine_m,
)
from .membership import Membership, MembershipChange, MembershipTracker
from .queries import SpatialQueries
from .service import SpatialService

__all__ = [
    "DEFAULT_H3_RESOLUTION",
    "CameraFootprint",
    "Membership",
    "MembershipChange",
    "MembershipTracker",
    "SpatialQueries",
    "SpatialService",
    "ZoneIndex",
    "ZoneShape",
    "cell_for",
    "cells_within",
    "haversine_m",
]
