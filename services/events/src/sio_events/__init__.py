"""SIO event engine (PRD M9, M22)."""

from .anomaly import AnomalyVerdict, FeatureVector, RobustZScoreDetector, build_detector
from .engine import Match, RuleEngine
from .facts import Fact, fact_from_entity, fact_from_event, fact_from_message
from .rules import Condition, Rule, RuleSet, load_rules
from .service import EventsService

__all__ = [
    "AnomalyVerdict",
    "Condition",
    "EventsService",
    "Fact",
    "FeatureVector",
    "Match",
    "RobustZScoreDetector",
    "Rule",
    "RuleEngine",
    "RuleSet",
    "build_detector",
    "fact_from_entity",
    "fact_from_event",
    "fact_from_message",
    "load_rules",
]
