"""SIO copilot (PRD M13, M20)."""

from .agent import AgentTrace, Answer, CopilotAgent, extract_arguments, route_by_keyword
from .evalset import EVAL_CASES, EvalCase, scripted_routes
from .service import CopilotService
from .tools import Tool, ToolBelt, ToolResult

__all__ = [
    "EVAL_CASES",
    "AgentTrace",
    "Answer",
    "CopilotAgent",
    "CopilotService",
    "EvalCase",
    "Tool",
    "ToolBelt",
    "ToolResult",
    "extract_arguments",
    "route_by_keyword",
    "scripted_routes",
]
