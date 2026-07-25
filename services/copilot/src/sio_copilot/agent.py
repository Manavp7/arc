"""The copilot's agent loop: plan, select tools, execute, synthesise, explain (PRD M13, M20).

Written as an explicit state machine rather than with LangGraph, and that is a deliberate deviation worth
defending. LangGraph earns its place when a graph has branches, cycles and human interrupts that a
hand-rolled loop would get wrong. This graph is five nodes and one bounded loop, and writing it out has
three concrete advantages here:

* **The degraded path is visible.** The single most important behaviour in this file is what happens when
  a small model fails to select a tool — and in a framework that logic hides inside a conditional edge.
* **Every step is inspectable.** `AgentTrace` records what was decided and why, which is what M20's
  explanation requirement actually needs. Reconstructing that from a framework's callbacks is more code
  than the loop itself.
* **No dependency between a demo and a framework release.** The `LLM` port is the seam that matters, and
  it is already swappable.

The loop is bounded by `max_steps`. Not a safety net — a load-bearing constraint. A small model asked a
vague question will happily call `list_entities` forever, and an agent with no step budget is a
denial-of-service against your own database.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from sio_core import get_logger
from sio_core.explain import ExplanationBuilder
from sio_core.llm import LlmReply, ToolCall, ToolSpec, validate_arguments
from sio_schemas import EvidenceKind, Explanation, utc_now

from .tools import ToolBelt, ToolResult

log = get_logger("sio.copilot.agent")

SYSTEM_PROMPT = """You are the operations copilot for a logistics yard monitored by cameras, GPS trackers \
and IoT sensors.

Rules:
- Answer using the tools. Never guess a number: if a tool did not return it, say you do not know.
- Call ONE tool at a time, then read the result before deciding whether you need another.
- When you have enough information, answer in two or three sentences, quoting the actual values.
- Be specific about time: say "in the last 5 minutes" rather than "recently".
- If a tool failed, say which one and answer with what you do have."""

SYNTHESIS_PROMPT = """Answer the user's question using only the tool results above.

Quote actual values. If the results do not contain the answer, say so plainly — do not fill the gap. Two \
or three sentences, no preamble, no bullet points."""


@dataclass
class Step:
    """One turn of the loop, recorded for the explanation."""

    index: int
    kind: str
    """``select`` | ``execute`` | ``synthesise`` | ``fallback``."""
    detail: str
    latency_ms: float = 0.0
    tool: str | None = None
    ok: bool = True


@dataclass
class AgentTrace:
    """Everything that happened, in order. The raw material for M20's explanation."""

    question: str
    steps: list[Step] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    model: str = ""
    total_ms: float = 0.0
    llm_ms: float = 0.0
    degraded: list[str] = field(default_factory=list)
    used_fallback: bool = False

    def add(self, step: Step) -> None:
        self.steps.append(step)

    @property
    def tools_used(self) -> list[str]:
        return [result.name for result in self.tool_results]

    def describe(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "model": self.model,
            "total_ms": round(self.total_ms, 1),
            "llm_ms": round(self.llm_ms, 1),
            "tools_used": self.tools_used,
            "used_fallback": self.used_fallback,
            "degraded": self.degraded,
            "steps": [
                {
                    "index": step.index,
                    "kind": step.kind,
                    "tool": step.tool,
                    "detail": step.detail,
                    "latency_ms": round(step.latency_ms, 1),
                    "ok": step.ok,
                }
                for step in self.steps
            ],
        }


@dataclass
class Answer:
    """What the copilot returns: prose, an explanation, and the trace behind both."""

    text: str
    explanation: Explanation
    trace: AgentTrace

    @property
    def confidence(self) -> float:
        return self.explanation.confidence


# --------------------------------------------------------------------- fallback
@dataclass(frozen=True)
class KeywordRoute:
    """A deterministic route used when the model cannot pick a tool.

    This is the belt-and-braces layer the PRD asks for, and it is the difference between "the copilot is
    broken" and "the copilot answered without the model's help". It is not a hidden success: every use is
    logged, counted, and stated in the explanation, because an answer produced without the model deserves
    to be labelled as such.
    """

    intent: str
    patterns: tuple[str, ...]
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    from_question: tuple[str, ...] = ()
    """Argument names to fill by extracting from the question text."""


KEYWORD_ROUTES: tuple[KeywordRoute, ...] = (
    KeywordRoute(
        intent="blind spots",
        patterns=(r"blind spot", r"not covered", r"no camera", r"coverage gap"),
        tool="spatial_query",
        arguments={"question": "blind_spots"},
    ),
    KeywordRoute(
        intent="cameras covering a zone",
        patterns=(r"camera.*(cover|watch|see|view)", r"(cover|watch).*camera"),
        tool="spatial_query",
        arguments={"question": "cameras_covering"},
        from_question=("zone_id",),
    ),
    KeywordRoute(
        intent="what happened earlier",
        patterns=(r"what happened", r"minutes ago", r"earlier", r"at \d{1,2}[:.]\d{2}", r"replay"),
        tool="timeline_replay",
        arguments={},
    ),
    KeywordRoute(
        intent="forecast",
        patterns=(
            r"will .* (be|get|become)",
            r"forecast",
            r"predict",
            r"trend",
            r"expect",
            r"congest",
            r"how busy",
            r"going to",
        ),
        tool="timeseries_query",
        arguments={"metric": "occupancy", "forecast": True},
    ),
    KeywordRoute(
        intent="frame search",
        patterns=(r"show me", r"footage", r"frame", r"image", r"picture", r"looked like"),
        tool="semantic_search",
        arguments={},
        from_question=("query",),
    ),
    KeywordRoute(
        intent="zone occupancy",
        patterns=(r"in (the )?(dock|gate|yard|apron|lane|warehouse|office|staging)", r"which zone"),
        tool="spatial_query",
        arguments={"question": "in_zone"},
        from_question=("zone_id",),
    ),
    KeywordRoute(
        intent="entity detail",
        patterns=(r"\bent_[0-9a-z]+\b",),
        tool="describe_entity",
        arguments={},
        from_question=("entity_id",),
    ),
    # Last, and the broadest: counting things on site is both the most common question and the safest
    # default, so an unrecognised question lands here rather than nowhere.
    KeywordRoute(
        intent="list entities",
        patterns=(
            r"how many",
            r"count",
            r"on site",
            r"right now",
            r"currently",
            r"\bare there\b",
            r".*",
        ),
        tool="list_entities",
        arguments={},
        from_question=("entity_type",),
    ),
)

ENTITY_TYPES = ("truck", "person", "people", "worker", "forklift", "drone", "vehicle")
ZONE_HINTS = (
    "gate_a",
    "gate_b",
    "dock_1",
    "dock_2",
    "dock_3",
    "dock_4",
    "dock_5",
    "dock_6",
    "yard",
    "apron",
    "staging",
    "warehouse",
    "office",
    "fuel_store",
    "lane_north",
    "lane_south",
)


def extract_arguments(question: str, wanted: tuple[str, ...]) -> dict[str, Any]:
    """Pull tool arguments out of the question text for the fallback path.

    Crude on purpose. This runs only when the model has already failed, so the bar is "better than
    nothing", not "as good as a model". Each extraction is one obvious rule, and anything not found is
    simply omitted so the tool applies its own default.
    """
    lowered = question.lower()
    found: dict[str, Any] = {}
    if "entity_type" in wanted:
        for candidate in ENTITY_TYPES:
            if candidate in lowered:
                normalised = {"people": "person", "worker": "person"}.get(candidate, candidate)
                found["entity_type"] = normalised
                break
    if "zone_id" in wanted:
        for zone in ZONE_HINTS:
            if zone in lowered or zone.replace("_", " ") in lowered:
                found["zone_id"] = zone
                break
        else:
            match = re.search(r"dock\s*(\d)", lowered)
            if match:
                found["zone_id"] = f"dock_{match.group(1)}"
    if "entity_id" in wanted:
        match = re.search(r"\b(ent_[0-9a-zA-Z]+)\b", question)
        if match:
            found["entity_id"] = match.group(1)
    if "query" in wanted:
        # Strip the request framing so the search gets the subject, not the verb.
        cleaned = re.sub(r"^(show me|find|search for|look for|give me)\s+", "", lowered).strip(
            " ?."
        )
        found["query"] = cleaned or question
    if "minutes_ago" in wanted:
        match = re.search(r"(\d+)\s*(minute|min)", lowered)
        if match:
            found["minutes_ago"] = float(match.group(1))
    return found


def route_by_keyword(question: str) -> KeywordRoute | None:
    lowered = question.lower()
    for route in KEYWORD_ROUTES:
        if any(re.search(pattern, lowered) for pattern in route.patterns):
            return route
    return None


# ------------------------------------------------------------------------ agent
class CopilotAgent:
    """Runs one question to an answer."""

    def __init__(
        self,
        llm: Any,
        belt: ToolBelt,
        *,
        max_steps: int = 4,
        allow_fallback: bool = True,
    ) -> None:
        self.llm = llm
        self.belt = belt
        self.max_steps = max_steps
        self.allow_fallback = allow_fallback
        self.answered = 0
        self.fallbacks = 0

    async def ask(self, question: str) -> Answer:
        started = time.perf_counter()
        trace = AgentTrace(question=question, model=getattr(self.llm, "model", "unknown"))
        # Side-effecting tools check the question themselves. A tool description is guidance a model may
        # ignore; a check in the tool is a control.
        self.belt.question = question
        tools = self.belt.by_name()
        specs = [tool.spec for tool in tools.values()]

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]

        for index in range(self.max_steps):
            reply = await self._select(messages, specs, trace, index)
            if reply.degraded:
                trace.degraded.append(reply.degraded)

            if not reply.wants_tools:
                if trace.tool_results:
                    break  # it has what it needs and wants to answer
                if reply.text.strip():
                    # The model answered directly, and that is a legitimate answer — "hello" and "what
                    # can you do" need no data. Falling back here would query the database to say hello,
                    # which is exactly the eager behaviour the restraint cases exist to prevent, and it
                    # would destroy the property on every model that gets restraint RIGHT.
                    trace.add(
                        Step(
                            index=index,
                            kind="synthesise",
                            detail="answered without tools, which the question did not need",
                            latency_ms=reply.latency_ms,
                        )
                    )
                    return self._direct(question, reply.text, trace, started)
                # Neither a tool nor any prose: the model declined to act at all. THIS is the failure the
                # fallback exists for, and it is the most common one below 4 B.
                if self.allow_fallback:
                    await self._fallback(question, trace, tools)
                break

            call = reply.tool_calls[0]
            tool = tools.get(call.name)
            if tool is None:
                # A hallucinated tool name. Do NOT guess a substitute: a wrong tool run confidently
                # produces a fluent answer about the wrong thing, which is the worst possible outcome.
                trace.degraded.append(
                    f"the model asked for a tool that does not exist: {call.name!r}"
                )
                trace.add(
                    Step(index=index, kind="select", detail=f"unknown tool {call.name!r}", ok=False)
                )
                if self.allow_fallback and not trace.tool_results:
                    await self._fallback(question, trace, tools)
                break

            valid, problem = validate_arguments(call, tool.spec)
            if not valid:
                trace.degraded.append(f"{call.name} called with bad arguments ({problem})")
                # Drop the offending keys and let the tool's own defaults apply, rather than abandoning a
                # correct tool choice over an argument the tool could have defaulted.
                call = ToolCall(
                    name=call.name,
                    arguments={
                        key: value
                        for key, value in call.arguments.items()
                        if key in tool.spec.parameters.get("properties", {})
                    },
                )

            result = await self._execute(call, tool, trace, index)
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": call.name, "arguments": call.arguments}}],
                }
            )
            messages.append({"role": "tool", "name": call.name, "content": result.for_model()})

            if result.ok and self._enough(question, trace):
                break

        text = await self._synthesise(messages, trace)
        explanation = self._explain(question, text, trace)
        trace.total_ms = (time.perf_counter() - started) * 1000
        self.answered += 1
        log.info(
            "copilot.answered",
            tools=trace.tools_used,
            fallback=trace.used_fallback,
            total_ms=round(trace.total_ms),
            llm_ms=round(trace.llm_ms),
            degraded=len(trace.degraded),
        )
        return Answer(text=text, explanation=explanation, trace=trace)

    def _direct(self, question: str, text: str, trace: AgentTrace, started: float) -> Answer:
        """An answer the model gave without needing data.

        Given its own small explanation rather than the tool-backed one: there is no evidence to cite, and
        a confident-looking explanation with an empty evidence list would misrepresent a conversational
        reply as a grounded finding.
        """
        builder = ExplanationBuilder(summary=text[:400])
        builder.add_model(trace.model, note="answered directly; the question needed no data")
        builder.add_note(
            "No tool was called because the question did not require site data. Nothing here is a "
            "measurement."
        )
        builder.confidence(0.5)
        trace.total_ms = (time.perf_counter() - started) * 1000
        self.answered += 1
        return Answer(text=text, explanation=builder.build(), trace=trace)

    # ------------------------------------------------------------------- nodes
    async def _select(
        self, messages: list[dict[str, Any]], specs: list[ToolSpec], trace: AgentTrace, index: int
    ) -> LlmReply:
        reply = await self.llm.chat(messages, tools=specs)
        trace.llm_ms += reply.latency_ms
        trace.add(
            Step(
                index=index,
                kind="select",
                detail=(
                    ", ".join(call.describe() for call in reply.tool_calls)
                    if reply.wants_tools
                    else (reply.text[:120] or "no tool selected")
                ),
                latency_ms=reply.latency_ms,
                tool=reply.tool_calls[0].name if reply.wants_tools else None,
            )
        )
        return reply

    async def _execute(
        self, call: ToolCall, tool: Any, trace: AgentTrace, index: int
    ) -> ToolResult:
        result = await self.belt._timed(call.name, tool.run(call.arguments))
        trace.tool_results.append(result)
        trace.add(
            Step(
                index=index,
                kind="execute",
                detail=result.error or f"{call.describe()} -> ok",
                latency_ms=result.latency_ms,
                tool=call.name,
                ok=result.ok,
            )
        )
        return result

    async def _fallback(self, question: str, trace: AgentTrace, tools: dict[str, Any]) -> None:
        """Answer without the model's help, and label it."""
        route = route_by_keyword(question)
        if route is None:
            return
        tool = tools.get(route.tool)
        if tool is None:
            return
        arguments = {**route.arguments, **extract_arguments(question, route.from_question)}
        trace.used_fallback = True
        self.fallbacks += 1
        trace.degraded.append(
            f"the model did not select a tool, so a deterministic keyword route was used "
            f"({route.intent} -> {route.tool})"
        )
        trace.add(
            Step(
                index=len(trace.steps),
                kind="fallback",
                detail=f"{route.intent} -> {route.tool}({arguments})",
                tool=route.tool,
            )
        )
        await self._execute(
            ToolCall(name=route.tool, arguments=arguments), tool, trace, len(trace.steps)
        )

    def _enough(self, question: str, trace: AgentTrace) -> bool:
        """Stop early when one good tool result plainly answers the question.

        A second round trip costs seconds on a local model, and for the common single-fact questions
        ("how many trucks") the extra turn adds nothing but latency. Multi-part questions are detected by
        conjunctions, which is crude but cheap and errs toward asking the model again.
        """
        if not trace.tool_results or not trace.tool_results[-1].ok:
            return False
        multipart = re.search(r"\b(and|then|also|compare)\b", question.lower()) is not None
        return not multipart

    async def _synthesise(self, messages: list[dict[str, Any]], trace: AgentTrace) -> str:
        results = [result for result in trace.tool_results if result.ok]
        if not results:
            failures = "; ".join(
                f"{result.name}: {result.error}" for result in trace.tool_results if result.error
            )
            # An honest refusal. Inventing an answer here is the single most damaging thing a copilot can
            # do, because it is indistinguishable from a correct one.
            return "I could not answer that: no tool returned usable data." + (
                f" ({failures})" if failures else ""
            )

        messages = [*messages, {"role": "user", "content": SYNTHESIS_PROMPT}]
        reply = await self.llm.chat(messages)
        trace.llm_ms += reply.latency_ms
        trace.add(
            Step(
                index=len(trace.steps),
                kind="synthesise",
                detail=reply.text[:160] or "(empty)",
                latency_ms=reply.latency_ms,
            )
        )
        if reply.text:
            return reply.text
        # The model produced nothing. Rather than an empty answer, state what was found — the data is the
        # valuable part and the prose is the wrapper.
        trace.degraded.append(
            "the model returned no prose, so the tool results are reported directly"
        )
        return summarise_results(results)

    def _explain(self, question: str, answer: str, trace: AgentTrace) -> Explanation:
        """Build the M20 explanation: evidence, sources, timeline, confidence, alternatives."""
        builder = ExplanationBuilder(summary=answer[:400] or "No answer produced")
        builder.add_model(trace.model, note=f"{len(trace.tool_results)} tool call(s)")

        for result in trace.tool_results:
            builder.add_note(
                f"{result.name} via {result.source or 'unknown source'}: "
                + ("ok" if result.ok else f"FAILED — {result.error}")
                + f" ({result.latency_ms:.0f} ms)"
            )
            for reference in result.evidence[:6]:
                if reference:
                    builder.add_evidence(EvidenceKind.QUERY, reference, source_id=result.name)
            if result.truncated:
                builder.add_note(
                    f"{result.name} returned more than the model's context allows; the answer is based "
                    "on a sample"
                )

        for note in trace.degraded:
            builder.add_note(f"degraded: {note}")
        if trace.used_fallback:
            builder.degraded(
                "the model did not choose a tool; a deterministic keyword route answered instead"
            )
        # Degraded when nothing SUCCEEDED, not merely when nothing was attempted. A tool that ran and
        # failed leaves a result in the trace, so the original check ("no results") passed happily for an
        # answer with no evidence behind it at all.
        if not any(result.ok for result in trace.tool_results):
            builder.degraded("no tool returned usable data, so this answer is not evidence-backed")

        # A real timeline with real timestamps: the entries are what happened, in order, and the kinds
        # match the schema's vocabulary so the shared explanation drawer can render them like any other.
        now = utc_now()
        builder.add_timeline(now, "note", f"asked: {question[:120]}")
        for step in trace.steps:
            builder.add_timeline(
                now,
                "action" if step.kind == "execute" else "note",
                f"{step.kind}: {step.detail[:120]}",
                ref=step.tool,
            )

        # Confidence from what actually happened, not from the model's opinion of itself. Evidence raises
        # it; every degradation lowers it; no evidence caps it low however fluent the prose.
        ok_tools = sum(1 for result in trace.tool_results if result.ok)
        if ok_tools == 0:
            builder.confidence(0.1)
        else:
            score = 0.55 + 0.15 * min(2, ok_tools) - 0.12 * len(trace.degraded)
            builder.confidence(max(0.15, min(0.92, score)))
        return builder.build()


def summarise_results(results: list[ToolResult]) -> str:
    """Report tool results as prose without a model. Used when the model returns nothing."""
    parts = []
    for result in results:
        data = result.data
        if isinstance(data, dict) and "count" in data:
            by_type = data.get("by_type") or {}
            detail = ", ".join(f"{count} {name}" for name, count in by_type.items())
            parts.append(f"{data['count']} entities on site" + (f" ({detail})" if detail else ""))
        elif isinstance(data, dict) and "summary" in data:
            parts.append(str(data["summary"]))
        elif isinstance(data, dict):
            keys = ", ".join(sorted(data)[:6])
            parts.append(f"{result.name} returned {keys}")
        else:
            parts.append(f"{result.name} returned {json.dumps(data, default=str)[:160]}")
    return ". ".join(parts) + "."


__all__ = [
    "AgentTrace",
    "Answer",
    "CopilotAgent",
    "KeywordRoute",
    "extract_arguments",
    "route_by_keyword",
]
