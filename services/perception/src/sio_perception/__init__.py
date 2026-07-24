"""SIO vision engine (PRD M3)."""

from .factory import build_detector, build_fire_detector, build_reid
from .redact import Redactor
from .service import PerceptionService

__all__ = [
    "PerceptionService",
    "Redactor",
    "build_detector",
    "build_fire_detector",
    "build_reid",
]
