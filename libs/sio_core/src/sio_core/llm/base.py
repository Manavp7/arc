"""The LLM seam: one small protocol, two adapters, and no framework in the middle.

A copilot that depends on a model is a copilot that cannot be tested. So everything above this layer
talks to `LLM`, and the two implementations are a real local model and a scripted one that returns
recorded decisions with no model at all.

**Tool calling is the capability that matters here, not fluency.** A 1.5-3 B model that chats well but
cannot reliably choose among nine tools presents to a user as a broken copilot, not as a weak model — the
failure is silent and looks like the product is wrong. So `LlmReply` carries structured tool calls as a
first-class field rather than leaving the caller to parse prose, and every adapter reports whether the
model actually produced them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ToolSpec:
    """A tool as offered to a model.

    Kept as plain data rather than a decorator-driven registry, because the same specification has to be
    serialised three ways — for Ollama, for an OpenAI-compatible endpoint, and for MCP — and a decorator
    that hides the schema makes the second and third harder than the first.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    """JSON Schema for the arguments object."""

    def to_openai(self) -> dict[str, Any]:
        """The shape Ollama and OpenAI-compatible endpoints both accept."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(self.parameters.get("required", ()))


@dataclass(frozen=True)
class ToolCall:
    """A model's request to run one tool."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        rendered = ", ".join(f"{key}={value!r}" for key, value in sorted(self.arguments.items()))
        return f"{self.name}({rendered})"


@dataclass
class LlmReply:
    """What a model said, and how much to trust the shape of it."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    latency_ms: float = 0.0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    degraded: str | None = None
    """Set when the reply had to be salvaged — malformed JSON repaired, or a fallback used.

    Carried through to the user-facing explanation rather than swallowed. A copilot that quietly
    degrades teaches its user to trust answers that do not deserve it.
    """
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def describe(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "latency_ms": round(self.latency_ms, 1),
            "tool_calls": [call.describe() for call in self.tool_calls],
            "text_chars": len(self.text),
            "degraded": self.degraded,
        }


@runtime_checkable
class LLM(Protocol):
    """The port. Anything that can answer a chat turn, optionally by calling tools."""

    name: str
    model: str

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[ToolSpec] | None = None,
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> LlmReply: ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...


def parse_tool_calls(payload: Any) -> tuple[list[ToolCall], str | None]:
    """Extract tool calls from a model's message, repairing what can be repaired.

    Small models get this wrong in a handful of predictable ways, and each is worth handling because the
    alternative is a copilot that appears to have no opinion:

    * arguments arriving as a JSON *string* rather than an object — extremely common;
    * a single call where the schema says a list;
    * arguments wrapped in markdown fences by a model that has been trained to show its work.

    Anything repaired is reported, never silently accepted, so the explanation can say the model needed
    help. What is *not* attempted is guessing a tool name: a wrong tool called confidently is worse than
    no tool call, because the answer will be fluent and false.
    """
    calls: list[ToolCall] = []
    degraded: str | None = None

    raw_calls = payload if isinstance(payload, list) else [payload]
    for entry in raw_calls:
        if not isinstance(entry, dict):
            continue
        function = entry.get("function", entry)
        name = function.get("name")
        if not name:
            continue
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            text = arguments.strip()
            if text.startswith("```"):
                text = text.strip("`")
                text = text.split("\n", 1)[-1] if "\n" in text else text
            try:
                arguments = json.loads(text) if text else {}
                degraded = degraded or "tool arguments arrived as a JSON string and were parsed"
            except json.JSONDecodeError:
                arguments = {}
                degraded = "tool arguments were not valid JSON and were dropped"
        if not isinstance(arguments, dict):
            arguments = {}
            degraded = degraded or "tool arguments were not an object and were dropped"
        calls.append(ToolCall(name=str(name), arguments=arguments))
    return calls, degraded


def validate_arguments(call: ToolCall, spec: ToolSpec) -> tuple[bool, str | None]:
    """Check a call against its schema, shallowly.

    Deliberately shallow: required keys present, and no unknown keys. Full JSON Schema validation would
    reject a model for using `500` where the schema says a number and it produced `"500"`, which is a
    coercion the tool can do and a rejection the user cannot understand. Types are coerced at the tool
    boundary instead, where the tool knows what it wants.
    """
    missing = [key for key in spec.required if key not in call.arguments]
    if missing:
        return False, f"missing required argument(s): {', '.join(missing)}"
    known = set(spec.parameters.get("properties", {}))
    unknown = [key for key in call.arguments if key not in known]
    if unknown and known:
        return False, f"unknown argument(s): {', '.join(sorted(unknown))}"
    return True, None
