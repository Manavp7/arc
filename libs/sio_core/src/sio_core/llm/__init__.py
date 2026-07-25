"""The LLM seam and its adapters."""

from .base import LLM, LlmReply, ToolCall, ToolSpec, parse_tool_calls, validate_arguments
from .ollama import OllamaLLM
from .scripted import Route, ScriptedLLM

__all__ = [
    "LLM",
    "LlmReply",
    "OllamaLLM",
    "Route",
    "ScriptedLLM",
    "ToolCall",
    "ToolSpec",
    "parse_tool_calls",
    "validate_arguments",
]
