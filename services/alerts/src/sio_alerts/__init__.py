"""SIO alerts (PRD M16)."""

from .scoring import (
    DEDUP_WINDOW_S,
    ESCALATE_AFTER_S,
    SEVERITY_WEIGHT,
    ZONE_CRITICALITY,
    Scored,
    group_key,
    recency_factor,
    score_alert,
    should_escalate,
    title_for,
    within_dedup_window,
    zone_criticality,
)
from .service import ALERTABLE, AlertsService

__all__ = [
    "ALERTABLE",
    "DEDUP_WINDOW_S",
    "ESCALATE_AFTER_S",
    "SEVERITY_WEIGHT",
    "ZONE_CRITICALITY",
    "AlertsService",
    "Scored",
    "group_key",
    "recency_factor",
    "score_alert",
    "should_escalate",
    "title_for",
    "within_dedup_window",
    "zone_criticality",
]
