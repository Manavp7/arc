"""ScriptedLLM: recorded decisions, no model.

The point of this adapter is that **CI must never depend on a model**. A test suite whose outcome moves
when a model is re-quantised is not a test suite, and a demo that fails because Ollama is not running is
not a demo.

So the scripted adapter answers from a table of intents keyed by what the question is *about*. It is not
a mock in the usual sense — it does not assert on being called, and it does not return a fixed blob. It
makes the same *decisions* a working model would make on the eval set: which tool, with which arguments,
and what to say once the results come back. That means the graph, the tool layer, the argument coercion,
the explanation builder and the API contract are all genuinely exercised, and only the token generation
is replaced.

Matching is keyword-based and deliberately transparent. A regex table that quietly mis-scores would be
worse than no fixture at all, so every route is one entry, in one list, in reading order, and the entry
that matched is reported in the reply.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..telemetry import get_logger
from .base import LlmReply, ToolCall, ToolSpec

log = get_logger("sio.llm.scripted")


@dataclass
class Route:
    """One scripted decision: what to match, which tools to call, and how to answer."""

    intent: str
    patterns: tuple[str, ...]
    tool_calls: list[ToolCall] = field(default_factory=list)
    answer: str = ""
    answer_from: Callable[[list[dict[str, Any]]], str] | None = None
    """Build the answer from the tool results, so the scripted path exercises real data.

    An answer that ignores the tool output would let a broken tool pass the eval, which is exactly
    backwards: the fixture exists to test everything *except* token generation.
    """
    arguments_from: Callable[[str], dict[str, Any]] | None = None
    """Derive tool arguments from the question, merged over the static ones.

    Added in Phase 8 because the eval harness found the scripted router answering the wrong question fluently:
    "How many trucks are on site?" matched the `list entities` route, whose arguments were a static `{}`, so it
    listed EVERYTHING. Tool selection scored 95%; argument accuracy scored 71%, and the gap was entirely this.

    That matters beyond the fixture. `ScriptedLLM` is what the copilot falls back to when the model is
    unreachable, and `copilot_allow_degraded` makes those answers user-visible — so a degraded copilot was
    answering "how many trucks" with the total count of everything on site. A wrong answer delivered
    confidently is the failure this platform spends most of its effort avoiding.
    """

    def matches(self, question: str) -> bool:
        lowered = question.lower()
        return any(re.search(pattern, lowered) for pattern in self.patterns)

    def calls_for(self, question: str) -> list[ToolCall]:
        """The tool calls for this question, with derived arguments merged over the static ones.

        Derived arguments WIN over static ones, because the static values are defaults for the general case and
        the derived ones are what this particular question asked for. A new ToolCall is built rather than
        mutating the route's own: routes are shared across every question, and mutating one would make the
        second question inherit the first's arguments.
        """
        if self.arguments_from is None:
            return list(self.tool_calls)
        derived = self.arguments_from(question)
        if not derived:
            return list(self.tool_calls)
        return [
            ToolCall(name=call.name, arguments={**call.arguments, **derived})
            for call in self.tool_calls
        ]


class ScriptedLLM:
    """A deterministic stand-in that makes real decisions from a table."""

    name = "scripted"

    def __init__(self, routes: list[Route] | None = None, *, model: str = "scripted") -> None:
        self.routes = routes if routes is not None else []
        self.model = model
        self.calls: list[dict[str, Any]] = []
        """Every request, for tests that want to assert what the graph asked for."""

    def add(self, route: Route) -> ScriptedLLM:
        self.routes.append(route)
        return self

    def route_for(self, question: str) -> Route | None:
        return next((route for route in self.routes if route.matches(question)), None)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[ToolSpec] | None = None,
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LlmReply:
        started = time.perf_counter()
        self.calls.append({"messages": messages, "tools": [tool.name for tool in tools or []]})

        question = _last_user_question(messages)
        route = self.route_for(question)
        elapsed = (time.perf_counter() - started) * 1000

        if route is None:
            # No route is a real answer: "I do not know" beats inventing one, and the eval should notice
            # a question the fixture does not cover rather than passing on a hallucination.
            return LlmReply(
                text="",
                model=self.model,
                latency_ms=elapsed,
                degraded=f"no scripted route matched: {question[:80]!r}",
            )

        # A synthesis turn — the graph has already run the tools and is asking for prose. Detected by the
        # presence of tool results in the conversation rather than by a flag, so the same script serves
        # both phases exactly as a real model would see them.
        results = _tool_results(messages)
        if results or not route.tool_calls:
            text = route.answer_from(results) if route.answer_from else route.answer
            return LlmReply(
                text=text, model=self.model, latency_ms=elapsed, raw={"intent": route.intent}
            )

        offered = {tool.name for tool in tools or []}
        # `calls_for`, not `route.tool_calls`: this is where a route's derived arguments are applied, and using
        # the raw list here would silently ignore every `arguments_from`.
        callable_now = [
            call for call in route.calls_for(question) if not offered or call.name in offered
        ]
        if not callable_now:
            return LlmReply(
                text=route.answer,
                model=self.model,
                latency_ms=elapsed,
                degraded=f"scripted route {route.intent!r} wanted tools that were not offered",
                raw={"intent": route.intent},
            )
        return LlmReply(
            tool_calls=callable_now,
            model=self.model,
            latency_ms=elapsed,
            raw={"intent": route.intent},
        )

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


def _last_user_question(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tool results already in the conversation, as the graph appends them."""
    results = []
    for message in messages:
        if message.get("role") == "tool":
            results.append(
                {
                    "name": message.get("name", ""),
                    "content": message.get("content", ""),
                }
            )
    return results


__all__ = ["Route", "ScriptedLLM"]
