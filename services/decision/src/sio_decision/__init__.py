"""SIO decision support (PRD M12)."""

from .recommend import STRATEGIES, build_decision, build_options, llm_rationale, template_rationale
from .service import ACTIONABLE, DecisionService
from .solvers import (
    Assignment,
    DockRequest,
    DockSlot,
    Incident,
    Responder,
    ScheduleResult,
    SolverResult,
    solve_assignment,
    solve_dock_schedule,
    solve_route,
    suitability,
)

__all__ = [
    "ACTIONABLE",
    "STRATEGIES",
    "Assignment",
    "DecisionService",
    "DockRequest",
    "DockSlot",
    "Incident",
    "Responder",
    "ScheduleResult",
    "SolverResult",
    "build_decision",
    "build_options",
    "llm_rationale",
    "solve_assignment",
    "solve_dock_schedule",
    "solve_route",
    "suitability",
    "template_rationale",
]
