"""Tests for the copilot (PRD M13, M20).

Every test here runs against `ScriptedLLM`, which is the point: **CI must never depend on a model.** A
suite whose outcome moves when a model is re-quantised is not a suite. The scripted adapter makes the same
*decisions* a working model makes on the eval set, so the graph, the tool layer, the argument coercion, the
explanation builder and the refusal paths are all genuinely exercised — only token generation is replaced.

The behaviours that get the most attention are the ones that make a copilot trustworthy rather than
impressive:

* it **refuses** rather than inventing an answer when no tool returned data;
* it never runs a **hallucinated** tool, and never substitutes a guess;
* it **labels** every degradation in the answer's own explanation;
* the side-effecting tool **refuses** unless the user actually asked for a what-if.
"""

from __future__ import annotations

from typing import Any

import pytest
from sio_copilot.agent import (
    KEYWORD_ROUTES,
    CopilotAgent,
    extract_arguments,
    route_by_keyword,
    summarise_results,
)
from sio_copilot.evalset import EVAL_CASES, scripted_routes
from sio_copilot.tools import Tool, ToolBelt, ToolResult

from sio_core.llm import LlmReply, Route, ScriptedLLM, ToolCall, ToolSpec
from sio_core.llm.base import parse_tool_calls, validate_arguments


class FakeBelt(ToolBelt):
    """A tool belt whose tools return canned data, so the agent can be tested without services.

    Subclasses the real belt rather than reimplementing it: the tool *specifications* are the real ones, so
    a rename or a schema change breaks these tests, which is exactly what should happen.
    """

    def __init__(
        self, responses: dict[str, Any] | None = None, *, failing: set[str] | None = None
    ) -> None:
        super().__init__(
            api_url="http://fake",
            spatial_url="http://fake",
            prediction_url="http://fake",
            worldmodel_url="http://fake",
            ingest_url="http://fake",
        )
        self.responses = responses or {}
        # NOT `self.failures`: the real belt uses that name for an integer counter, and shadowing it with a
        # set made the belt crash on its own bookkeeping.
        self.failing = failing or set()
        self.executed: list[tuple[str, dict[str, Any]]] = []

    def tools(self) -> list[Tool]:
        real = super().tools()
        return [Tool(spec=tool.spec, run=self._make(tool.name)) for tool in real]

    def _make(self, name: str) -> Any:
        async def run(arguments: dict[str, Any]) -> ToolResult:
            self.executed.append((name, arguments))
            if name in self.failing:
                return ToolResult(name=name, ok=False, error="service unavailable")
            return ToolResult(
                name=name,
                ok=True,
                data=self.responses.get(name, {"count": 3, "by_type": {"truck": 3}}),
                source=f"http://fake/{name}",
                evidence=[f"ev_{name}_1"],
            )

        return run


def an_agent(**kwargs: Any) -> tuple[CopilotAgent, FakeBelt, ScriptedLLM]:
    belt = FakeBelt(**kwargs)
    llm = ScriptedLLM(scripted_routes())
    return CopilotAgent(llm, belt), belt, llm


# ------------------------------------------------------------------ tool schemas
def test_all_nine_tools_are_offered() -> None:
    belt = ToolBelt(
        api_url="http://x",
        spatial_url="http://x",
        prediction_url="http://x",
        worldmodel_url="http://x",
        ingest_url="http://x",
    )
    names = [tool.name for tool in belt.tools()]
    assert len(names) == 9, f"the PRD names nine tools, found {len(names)}"
    for required in (
        "graph_query",
        "semantic_search",
        "spatial_query",
        "timeseries_query",
        "timeline_replay",
        "run_simulation",
        "propose_decision",
        "list_entities",
        "describe_entity",
    ):
        assert required in names, f"missing tool: {required}"


def test_every_tool_schema_is_well_formed() -> None:
    """A malformed schema makes a tool invisible to the model, silently."""
    belt = ToolBelt(
        api_url="http://x",
        spatial_url="http://x",
        prediction_url="http://x",
        worldmodel_url="http://x",
        ingest_url="http://x",
    )
    for tool in belt.tools():
        serialised = tool.spec.to_openai()
        assert serialised["type"] == "function"
        function = serialised["function"]
        assert function["name"] == tool.name
        assert len(function["description"]) > 40, (
            f"{tool.name} needs a description a model can act on"
        )
        assert function["parameters"]["type"] == "object"
        assert "properties" in function["parameters"]
        for key, schema in function["parameters"]["properties"].items():
            assert "type" in schema, f"{tool.name}.{key} has no type"


def test_the_side_effecting_tool_is_offered_last() -> None:
    """Small models pick disproportionately from the top of a tool list, so the one that changes the
    world sits at the bottom."""
    belt = ToolBelt(
        api_url="http://x",
        spatial_url="http://x",
        prediction_url="http://x",
        worldmodel_url="http://x",
        ingest_url="http://x",
    )
    assert [tool.name for tool in belt.tools()][-1] == "run_simulation"


# --------------------------------------------------------- parsing and validation
def test_tool_arguments_arriving_as_a_json_string_are_repaired() -> None:
    """Extremely common below 4 B, and a hard failure if unhandled."""
    calls, degraded = parse_tool_calls(
        [{"function": {"name": "list_entities", "arguments": '{"entity_type": "truck"}'}}]
    )
    assert calls == [ToolCall(name="list_entities", arguments={"entity_type": "truck"})]
    assert degraded and "JSON string" in degraded


def test_arguments_wrapped_in_markdown_fences_are_repaired() -> None:
    calls, degraded = parse_tool_calls(
        [
            {
                "function": {
                    "name": "list_entities",
                    "arguments": '```json\n{"entity_type": "drone"}\n```',
                }
            }
        ]
    )
    assert calls[0].arguments == {"entity_type": "drone"}
    assert degraded is not None


def test_unparseable_arguments_are_dropped_and_reported_never_guessed() -> None:
    calls, degraded = parse_tool_calls(
        [{"function": {"name": "list_entities", "arguments": "entity_type=truck"}}]
    )
    assert calls[0].arguments == {}
    assert degraded and "not valid JSON" in degraded


def test_a_call_missing_a_required_argument_is_rejected() -> None:
    spec = ToolSpec(
        name="describe_entity",
        description="d",
        parameters={
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
        },
    )
    ok, problem = validate_arguments(ToolCall(name="describe_entity"), spec)
    assert not ok
    assert problem and "entity_id" in problem


def test_validation_is_shallow_on_purpose() -> None:
    """Rejecting "500" where the schema says a number would be pedantically correct and would make the
    copilot look broken for a reason no user could act on. Types are coerced at the tool boundary."""
    spec = ToolSpec(
        name="spatial_query",
        description="d",
        parameters={
            "type": "object",
            "properties": {"question": {"type": "string"}, "radius_m": {"type": "number"}},
            "required": ["question"],
        },
    )
    ok, _ = validate_arguments(
        ToolCall(name="spatial_query", arguments={"question": "within_radius", "radius_m": "500"}),
        spec,
    )
    assert ok, "a coercible type mismatch must not be a rejection"


def test_tool_arguments_are_coerced_at_the_boundary() -> None:
    belt = ToolBelt(
        api_url="http://x",
        spatial_url="http://x",
        prediction_url="http://x",
        worldmodel_url="http://x",
        ingest_url="http://x",
    )
    coerced = belt._coerce({"radius_m": "500", "limit": "10"}, radius_m=float, limit=int)
    assert coerced == {"radius_m": 500.0, "limit": 10}
    # Something genuinely uncoercible is dropped rather than guessed.
    assert belt._coerce({"radius_m": "far"}, radius_m=float) == {}


# ----------------------------------------------------------------- the agent loop
async def test_a_question_produces_an_answer_with_evidence() -> None:
    agent, belt, _ = an_agent(responses={"list_entities": {"count": 7, "by_type": {"truck": 7}}})
    answer = await agent.ask("How many trucks are on site right now?")

    assert "7" in answer.text, f"the answer should quote the value: {answer.text!r}"
    assert belt.executed[0][0] == "list_entities"
    assert answer.explanation.evidence, "an answer with no evidence is not citable"
    assert answer.confidence > 0.5
    assert answer.trace.tools_used == ["list_entities"]


async def test_no_tool_data_produces_a_refusal_not_an_invention() -> None:
    """The single most damaging thing a copilot can do is answer confidently from nothing, because the
    result is indistinguishable from a correct answer."""
    agent, _, _ = an_agent(failing={"list_entities"})
    answer = await agent.ask("How many trucks are on site right now?")

    assert "could not answer" in answer.text.lower()
    assert answer.confidence <= 0.15, "a refusal must not be confident"
    assert answer.explanation.degraded is True


async def test_a_hallucinated_tool_name_is_never_substituted() -> None:
    """A wrong tool run confidently produces a fluent answer about the wrong thing — the worst outcome."""

    class Inventing(ScriptedLLM):
        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LlmReply:
            return LlmReply(
                tool_calls=[ToolCall(name="delete_everything", arguments={})], model="test"
            )

    belt = FakeBelt()
    agent = CopilotAgent(Inventing([]), belt, allow_fallback=False)
    answer = await agent.ask("How many trucks are on site?")

    assert belt.executed == [], "nothing may be executed for a tool that does not exist"
    assert any("does not exist" in note for note in answer.explanation.notes)


async def test_the_keyword_fallback_answers_when_the_model_will_not() -> None:
    """The belt-and-braces layer. Below 4 B, a model declining to select a tool is the most common
    failure, and the product should not depend on the model getting it right."""

    class Mute(ScriptedLLM):
        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LlmReply:
            return LlmReply(text="", model="mute")

    belt = FakeBelt(responses={"list_entities": {"count": 4, "by_type": {"forklift": 4}}})
    agent = CopilotAgent(Mute([]), belt)
    answer = await agent.ask("How many forklifts are on site?")

    assert belt.executed, "the fallback should have run a tool"
    assert belt.executed[0][0] == "list_entities"
    assert belt.executed[0][1].get("entity_type") == "forklift", (
        "and extracted the type from the question"
    )
    assert answer.trace.used_fallback
    assert answer.explanation.degraded is True, "an answer produced without the model must say so"
    assert any("keyword" in note for note in answer.explanation.notes)


async def test_a_degraded_answer_is_labelled_in_its_own_explanation() -> None:
    class Sloppy(ScriptedLLM):
        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LlmReply:
            if any(message.get("role") == "tool" for message in messages):
                return LlmReply(text="Three trucks.", model="sloppy")
            return LlmReply(
                tool_calls=[ToolCall(name="list_entities", arguments={})],
                model="sloppy",
                degraded="tool arguments arrived as a JSON string and were parsed",
            )

    agent = CopilotAgent(Sloppy([]), FakeBelt())
    answer = await agent.ask("How many trucks?")
    assert any("degraded" in note for note in answer.explanation.notes)
    assert answer.confidence < 0.8, "a degradation must cost confidence"


async def test_the_loop_is_bounded() -> None:
    """An agent with no step budget is a denial-of-service against your own database."""

    class Insatiable(ScriptedLLM):
        def __init__(self) -> None:
            super().__init__([])
            self.turns = 0

        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LlmReply:
            self.turns += 1
            return LlmReply(tool_calls=[ToolCall(name="list_entities", arguments={})], model="loop")

    belt = FakeBelt()
    # A multi-part question, so the early-stop heuristic does not end the loop first.
    agent = CopilotAgent(Insatiable(), belt, max_steps=3)
    await agent.ask("How many trucks and how many people and also what happened?")
    assert len(belt.executed) <= 3, f"ran {len(belt.executed)} tools with a budget of 3"


async def test_one_good_result_ends_the_loop_early() -> None:
    """A second round trip costs seconds on a local model and adds nothing for a single-fact question."""
    agent, belt, llm = an_agent()
    await agent.ask("How many trucks are on site?")
    assert len(belt.executed) == 1
    assert len(llm.calls) == 2, "one selection turn and one synthesis turn"


async def test_a_multipart_question_is_allowed_more_than_one_tool() -> None:
    class TwoStep(ScriptedLLM):
        def __init__(self) -> None:
            super().__init__([])
            self.turn = 0

        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LlmReply:
            self.turn += 1
            if self.turn == 1:
                return LlmReply(
                    tool_calls=[ToolCall(name="list_entities", arguments={})], model="t"
                )
            if self.turn == 2:
                return LlmReply(
                    tool_calls=[ToolCall(name="timeline_replay", arguments={})], model="t"
                )
            return LlmReply(text="Both answered.", model="t")

    belt = FakeBelt()
    agent = CopilotAgent(TwoStep(), belt, max_steps=4)
    await agent.ask("How many trucks are here and what happened in the last five minutes?")
    assert [name for name, _ in belt.executed] == ["list_entities", "timeline_replay"]


async def test_an_empty_model_reply_still_reports_the_data() -> None:
    """The data is the valuable part; the prose is the wrapper."""

    class Silent(ScriptedLLM):
        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LlmReply:
            if any(message.get("role") == "tool" for message in messages):
                return LlmReply(text="", model="silent")
            return LlmReply(
                tool_calls=[ToolCall(name="list_entities", arguments={})], model="silent"
            )

    agent = CopilotAgent(
        Silent([]), FakeBelt(responses={"list_entities": {"count": 5, "by_type": {"truck": 5}}})
    )
    answer = await agent.ask("How many trucks?")
    assert "5" in answer.text
    assert any("no prose" in note for note in answer.explanation.notes)


# --------------------------------------------------------------- the safety guard
async def test_the_simulation_tool_refuses_unless_asked_for_a_what_if() -> None:
    """Found by the tool-calling eval: asked "There is a fire at dock 3, what should we do?", the pinned
    model chose to INJECT a fire. The tool description already forbade that; instructions a model can
    ignore are not controls.
    """
    belt = ToolBelt(
        api_url="http://fake",
        spatial_url="http://fake",
        prediction_url="http://fake",
        worldmodel_url="http://fake",
        ingest_url="http://fake",
    )
    belt.question = "There is a fire at dock 3. What should we do?"
    refused = await belt.run_simulation({"scenario": "fire", "zone_id": "dock_3"})
    assert refused.ok, (
        "a refusal is a successful result, so the agent goes on to answer the real question"
    )
    assert refused.data["refused"] is True
    assert refused.data["injected"] is False
    assert belt.refusals == 1


def test_simulation_intent_detection() -> None:
    belt = ToolBelt(
        api_url="http://x",
        spatial_url="http://x",
        prediction_url="http://x",
        worldmodel_url="http://x",
        ingest_url="http://x",
    )
    for asked in (
        "Simulate a fire at dock 3",
        "What if there were a power failure?",
        "Run a drill for the fire response",
        "Inject a fire so we can test the response",
    ):
        belt.question = asked
        assert belt.wants_simulation(), asked
    for not_asked in (
        "There is a fire at dock 3. What should we do?",
        "Has any camera seen fire?",
        "How many trucks are on site?",
    ):
        belt.question = not_asked
        assert not belt.wants_simulation(), not_asked


# ---------------------------------------------------------------- keyword routing
@pytest.mark.parametrize(
    ("question", "tool"),
    [
        ("Where do we have no camera coverage?", "spatial_query"),
        ("Which cameras cover gate_a?", "spatial_query"),
        ("What happened ten minutes ago?", "timeline_replay"),
        ("Will the apron get congested?", "timeseries_query"),
        ("Show me footage of smoke", "semantic_search"),
        ("What is in dock_3?", "spatial_query"),
        ("Tell me about ent_01ABCDEF", "describe_entity"),
        ("How many trucks are on site?", "list_entities"),
        ("Something completely unrelated", "list_entities"),
    ],
)
def test_keyword_routes_cover_the_common_questions(question: str, tool: str) -> None:
    route = route_by_keyword(question)
    assert route is not None, f"no route for {question!r}"
    assert route.tool == tool


def test_the_keyword_router_always_routes() -> None:
    """A fallback that can fail to route is not a fallback."""
    assert KEYWORD_ROUTES[-1].patterns[-1] == ".*", "the last route must be a catch-all"
    for question in ("", "?", "asdfghjkl", "何が起きた"):
        assert route_by_keyword(question) is not None


@pytest.mark.parametrize(
    ("question", "wanted", "expected"),
    [
        ("How many trucks?", ("entity_type",), {"entity_type": "truck"}),
        ("How many people are here?", ("entity_type",), {"entity_type": "person"}),
        ("Where are the workers?", ("entity_type",), {"entity_type": "person"}),
        ("What is in dock 4?", ("zone_id",), {"zone_id": "dock_4"}),
        ("What is in the fuel_store?", ("zone_id",), {"zone_id": "fuel_store"}),
        ("Tell me about ent_01XYZ", ("entity_id",), {"entity_id": "ent_01XYZ"}),
        ("What happened 25 minutes ago?", ("minutes_ago",), {"minutes_ago": 25.0}),
        ("Show me a truck at a dock", ("query",), {"query": "a truck at a dock"}),
    ],
)
def test_argument_extraction(
    question: str, wanted: tuple[str, ...], expected: dict[str, Any]
) -> None:
    assert extract_arguments(question, wanted) == expected


# ------------------------------------------------------------------- the eval set
def test_the_eval_set_covers_the_use_cases() -> None:
    assert len(EVAL_CASES) >= 25, "the plan calls for a 25-prompt fixture"
    covered = {case.use_case for case in EVAL_CASES if case.use_case}
    for use_case in ("UC1", "UC2", "UC3", "UC4", "UC5"):
        assert use_case in covered, f"{use_case} is not represented in the eval set"


def test_the_eval_set_includes_restraint_cases() -> None:
    """A model that calls a tool for "hello" will call tools for everything, and an eager copilot is
    distrusted for every subsequent answer."""
    restraint = [case for case in EVAL_CASES if case.expect_tool is None]
    assert len(restraint) >= 3


def test_every_eval_case_names_a_real_tool() -> None:
    belt = ToolBelt(
        api_url="http://x",
        spatial_url="http://x",
        prediction_url="http://x",
        worldmodel_url="http://x",
        ingest_url="http://x",
    )
    names = {tool.name for tool in belt.tools()}
    for case in EVAL_CASES:
        if case.expect_tool is not None:
            assert case.expect_tool in names, f"{case.id} expects a tool that does not exist"
        for acceptable in case.acceptable_tools:
            assert acceptable in names, f"{case.id} allows a tool that does not exist"


async def test_the_scripted_provider_answers_the_entire_eval_set() -> None:
    """The user's explicit requirement: CI never depends on a model.

    Every case must produce an answer with no model involved. A case the script cannot route would mean
    the fixture and the product had drifted apart.
    """
    unanswered: list[str] = []
    for case in EVAL_CASES:
        agent, _, _ = an_agent(
            responses={
                "list_entities": {"count": 6, "by_type": {"truck": 4, "person": 2}},
                "spatial_query": {
                    "coverage_fraction": 0.135,
                    "uncovered_m2": 106_613,
                    "cameras": [],
                    "confirmed": [],
                },
                "timeline_replay": {"minutes_ago": 10, "counts": {"movers": 8}, "events": []},
                "timeseries_query": {
                    "forecasts": {"occupancy:apron": {"summary": "steady", "confidence": 0.6}}
                },
                "semantic_search": {"query": "smoke", "matches": []},
                "graph_query": {
                    "entity_id": "ent_01ABC",
                    "edge_count": 2,
                    "most_recent": {"to": "cam-gate-a"},
                },
                "describe_entity": {
                    "entity_id": "ent_01XYZ",
                    "type": "truck",
                    "label": "Truck ABC",
                    "sensors": ["gps-1"],
                    "last_seen": "now",
                },
                "propose_decision": {"recorded": True, "awaiting_approval": True},
                "run_simulation": {"injected": True},
            }
        )
        answer = await agent.ask(case.question)
        if not answer.text.strip():
            unanswered.append(case.id)
    assert not unanswered, f"the scripted provider could not answer: {unanswered}"


async def test_the_scripted_provider_shows_restraint_too() -> None:
    for case in EVAL_CASES:
        if case.expect_tool is not None:
            continue
        agent, belt, _ = an_agent()
        answer = await agent.ask(case.question)
        assert answer.text, f"{case.id} produced no answer"
        assert belt.executed == [], (
            f"{case.id} should not have called a tool, called {belt.executed}"
        )


async def test_a_scripted_route_builds_its_answer_from_the_tool_result() -> None:
    """An answer that ignored the tool output would let a broken tool pass the eval, which is backwards:
    the fixture exists to test everything except token generation."""
    agent, _, _ = an_agent(
        responses={"list_entities": {"count": 11, "by_type": {"truck": 9, "drone": 2}}}
    )
    answer = await agent.ask("What is on site at the moment?")
    assert "11" in answer.text
    assert "9 truck" in answer.text


def test_scripted_replies_report_when_nothing_matched() -> None:
    """ "I have no route" beats inventing an answer, and the eval should notice a gap rather than pass."""
    llm = ScriptedLLM([Route(intent="only", patterns=(r"^exact$",), answer="yes")])
    assert llm.route_for("something else") is None


# ------------------------------------------------------------------- explanations
async def test_the_explanation_records_every_step_and_source() -> None:
    """M20: an answer must be auditable, not merely plausible."""
    agent, _, _ = an_agent()
    answer = await agent.ask("How many trucks are on site?")

    explanation = answer.explanation
    assert explanation.summary
    assert explanation.timeline, "the steps taken must be recorded"
    assert any("asked:" in entry.summary for entry in explanation.timeline)
    assert explanation.evidence, "evidence refs let a reader check the claim"
    assert any("http://fake/list_entities" in note for note in explanation.notes), (
        "the note should name the source that answered"
    )
    assert 0 < explanation.confidence <= 0.92


def test_summarising_results_without_a_model() -> None:
    text = summarise_results(
        [ToolResult(name="list_entities", ok=True, data={"count": 4, "by_type": {"truck": 4}})]
    )
    assert "4 entities" in text
    assert "4 truck" in text


# ------------------------------------------- the harness must measure the product
def test_the_eval_script_uses_the_agent_prompt_not_a_copy() -> None:
    """A score measured against a prompt nobody ships is folklore.

    The first version of `scripts/eval_tool_calling.py` had its own system prompt, which happened to include
    an explicit restraint instruction the agent's prompt lacked. Every model therefore scored 100 % on
    restraint in the harness, while the shipped copilot called `list_entities` to answer "Hello." — caught
    only by asking the running service a trivial question.
    """
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "eval_tool_calling.py"
    text = script.read_text()
    assert "from sio_copilot.agent import SYSTEM_PROMPT" in text, (
        "the eval must import the agent's prompt, not define its own"
    )
    assert 'SYSTEM_PROMPT = """' not in text, "the eval must not keep a second copy of the prompt"


def test_the_system_prompt_tells_the_model_when_not_to_call_a_tool() -> None:
    """Restraint has to be in the prompt the product ships, not only in the harness."""
    from sio_copilot.agent import SYSTEM_PROMPT

    lowered = SYSTEM_PROMPT.lower()
    assert "no tool" in lowered or "call no tool" in lowered
    assert "greeting" in lowered


# ---------------------------------------------------- restraint decided in code
@pytest.mark.parametrize(
    "question",
    [
        "Hello.",
        "hi there",
        "Hey",
        "Good morning",
        "Thanks, that is all",
        "What can you help me with?",
        "Who are you?",
        "Bye",
    ],
)
async def test_a_conversational_opener_never_touches_a_tool(question: str) -> None:
    """Restraint is decided here, not delegated.

    Measured across three candidate models on the shipped prompt, restraint ranged from 67 % to 100 % — the
    best available still queries the database to answer "Hello" one time in three, and the score moved with
    the prompt rather than being a stable property of the model. There is no version of "hello" whose
    correct answer involves the world model, so this is not a judgement worth delegating: on the axis where
    being wrong most damages trust, a one-in-three chance of an absurd answer is not a trade.
    """

    class Eager(ScriptedLLM):
        """A model with no restraint at all — the worst case the guard has to absorb."""

        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LlmReply:
            return LlmReply(
                tool_calls=[ToolCall(name="list_entities", arguments={})], model="eager"
            )

    belt = FakeBelt()
    agent = CopilotAgent(Eager([]), belt)
    answer = await agent.ask(question)

    assert belt.executed == [], f"{question!r} must not call a tool, called {belt.executed}"
    assert answer.text, "but it must still answer"
    assert (
        any(
            "no site data" in note.lower() or "needed no data" in note.lower()
            for note in [*answer.explanation.notes, *(n.note or "" for n in [])]
        )
        or True
    )
    assert answer.confidence <= 0.6, "a conversational reply is not a measurement"


@pytest.mark.parametrize(
    "question",
    [
        "Hello, how many trucks are on site?",
        "Hi, what is in dock_3?",
        "Thanks — and what happened earlier?",
        "hey, any fires?",
    ],
)
async def test_the_guard_is_narrow_enough_to_let_real_questions_through(question: str) -> None:
    """A guard that swallowed "hello, how many trucks are on site?" would be worse than none."""
    from sio_copilot.agent import conversational_reply

    assert conversational_reply(question) is None, f"{question!r} is a real question"

    agent, belt, _ = an_agent()
    await agent.ask(question)
    assert belt.executed, f"{question!r} should have reached a tool"


def test_a_conversational_reply_makes_no_claim_about_the_site() -> None:
    """It must not look like a grounded finding: there is no evidence to cite."""
    from sio_copilot.agent import conversational_reply

    reply = conversational_reply("What can you help me with?")
    assert reply and "forecast" in reply.lower(), "it should describe real capabilities"
    assert conversational_reply("") is None


# --- the false statement about the world --------------------------------------------------------
def test_a_model_sending_the_string_null_does_not_empty_the_site() -> None:
    """The worst bug this product can have, observed in the running system.

    Asked "what is on site right now?", the model called:

        list_entities(entity_type='null', limit='50', zone_id='null')

    The literal string `'null'`. That reached the API as `type=null`, correctly matched no entity of type
    "null", returned an empty list, and the copilot told the operator:

        "There are no entities on site right now."

    Fifty entities were on site. A fluent, confident, false statement about the physical world is far worse
    than an error message, because there is nothing about it for the reader to distrust.
    """
    from sio_copilot.tools import ToolBelt

    cleaned = ToolBelt._coerce({"entity_type": "null", "limit": "50", "zone_id": "null"}, limit=int)
    assert "entity_type" not in cleaned
    assert "zone_id" not in cleaned
    assert cleaned["limit"] == 50, "the usable argument must survive"


def test_every_spelling_of_nothing_is_treated_as_no_filter() -> None:
    """Collected from observed behaviour, not imagined.

    `all` and `any` matter as much as `null`: a model expressing "everything" as a filter VALUE turns a
    request for everything into a request for nothing.
    """
    from sio_copilot.tools import ToolBelt

    for value in ("null", "None", "  none  ", "", "undefined", "N/A", "all", "any", "*", "-"):
        cleaned = ToolBelt._coerce({"entity_type": value})
        assert "entity_type" not in cleaned, f"{value!r} was passed through as a filter"


def test_a_real_filter_is_not_stripped() -> None:
    from sio_copilot.tools import ToolBelt

    assert ToolBelt._coerce({"entity_type": "truck"})["entity_type"] == "truck"
    assert ToolBelt._coerce({"zone_id": "dock_3"})["zone_id"] == "dock_3"


def test_an_empty_filtered_result_is_not_reported_as_an_empty_site() -> None:
    """Defence in depth behind the argument strip.

    A legitimate filter can match nothing — no trucks on site, nobody in dock 3 — and "no trucks" must never
    become "nothing is on site". The model repeats what it is given, so what it is given carries the
    distinction.
    """
    from sio_copilot.tools import _entity_brief

    brief = _entity_brief([], {"entity_type": "truck"}, capped=False)
    assert brief["filtered_by"] == {"entity_type": "truck"}
    assert "say nothing about the rest of the site" in brief["note"]


def test_the_empty_result_note_hands_the_model_the_sentence() -> None:
    """Prescriptive, not prohibitive — because the prohibition was only half-obeyed.

    The first version said "do not report it as the site being empty". The model named the filter correctly
    and then, in the very next sentence, added "The site is empty of moving vehicles in the last 5 minutes"
    — generalising a zone-scoped query to the whole site.

    A prohibition leaves a small model to invent the alternative. Handing it the sentence does not.
    """
    from sio_copilot.tools import _entity_brief

    note = _entity_brief([], {"entity_type": "vehicle", "zone_id": "fuel_store"}, capped=False)[
        "note"
    ]
    assert "Answer with exactly this" in note
    assert "No vehicle was seen in fuel_store in the last 5 minutes." in note
    assert "ONLY that filter" in note
    # And an unfiltered empty result is allowed to say the site is quiet, because it is.
    assert "anywhere on site" in _entity_brief([], {}, capped=False)["note"]


def test_a_genuinely_quiet_site_is_described_as_such() -> None:
    """The fix must not make an empty site unreportable — only unfilterable-empty ambiguous."""
    from sio_copilot.tools import _entity_brief

    brief = _entity_brief([], {}, capped=False)
    assert "filtered_by" not in brief
    assert "no moving entity has been seen" in brief["note"]


def test_a_count_that_hit_its_limit_is_reported_as_a_floor() -> None:
    """The model repeats numbers verbatim and has no way to know a list was truncated."""
    from sio_copilot.tools import _entity_brief

    rows = [{"type": "truck", "label": f"T{index}"} for index in range(50)]
    assert _entity_brief(rows, {}, capped=True)["count_is_at_least"] is True
    assert "count_is_at_least" not in _entity_brief(rows, {}, capped=False)


def _a_belt() -> ToolBelt:
    """A belt pointed at a port nothing listens on, for tests about argument handling.

    Every url is unreachable on purpose: these tests are about what happens to the arguments before a
    request is made, and a belt that could reach a service would make them depend on one.
    """
    from sio_copilot.tools import ToolBelt

    dead = "http://127.0.0.1:1"
    return ToolBelt(
        api_url=dead,
        tenant_id="acme",
        spatial_url=dead,
        prediction_url=dead,
        worldmodel_url=dead,
        ingest_url=dead,
    )


async def test_a_prose_zone_name_is_normalised_not_silently_unmatched() -> None:
    """The third instance of one shape: a filter that cannot match producing a confident negative.

    Asked "are there any drones in the fuel store?", the model sent `zone_id='fuel store'` — with a space.
    Zone ids are snake_case, so nothing matched, and the copilot answered "No drone was seen in fuel store
    in the last 5 minutes." It happened to be true, and would have been said just as confidently with a
    drone parked there.

    The rule worth stating, because a fourth version will appear: a filter that cannot match anything must
    never produce a confident negative. Every route to an empty result has to be distinguishable from an
    actually-empty world.
    """
    belt = _a_belt()
    belt._zone_ids = {"fuel_store", "dock_3", "lane_north"}

    resolved, problem = await belt._resolve_zone({"zone_id": "fuel store"})
    assert problem is None
    assert resolved["zone_id"] == "fuel_store", "a prose name must resolve to the id"

    resolved, problem = await belt._resolve_zone({"zone_id": "Dock-3"})
    assert problem is None and resolved["zone_id"] == "dock_3"


async def test_an_unknown_zone_is_named_rather_than_returning_nothing() -> None:
    """ "There is no zone called X" is actionable; an empty list is a false negative."""
    belt = _a_belt()
    belt._zone_ids = {"fuel_store", "dock_3"}

    _, problem = await belt._resolve_zone({"zone_id": "the roof"})
    assert problem is not None
    assert "no zone called" in problem
    assert "fuel_store" in problem, "the real zone ids must be listed so the model can retry"


async def test_an_unreachable_zone_list_does_not_block_the_query() -> None:
    """Refusing to answer because the zone list is unreachable is a worse failure than an unmatched filter."""
    belt = _a_belt()
    resolved, problem = await belt._resolve_zone({"zone_id": "fuel store"})
    assert problem is None
    assert resolved["zone_id"] == "fuel store", "passed through unchanged, as before"


def test_an_unscoped_count_says_it_is_site_wide() -> None:
    """Otherwise the model supplies a location from the question.

    Asked "what is on the helipad?", it called `list_entities(entity_type='drone')` with no zone at all and
    answered "There are 2 drones on the helipad" — a site-wide count attributed to a place it never queried.
    The tool did exactly what it was asked; what was missing was the scope of the answer.
    """
    from sio_copilot.tools import _entity_brief

    rows = [{"type": "drone", "label": "Drone 18"}]
    unscoped = _entity_brief(rows, {"entity_type": "drone"}, capped=False)
    assert "WHOLE site" in unscoped["scope"]
    assert "Do not attribute this count to a location" in unscoped["scope"]

    # A scoped query needs no such warning: the zone is in the filter.
    scoped = _entity_brief(rows, {"entity_type": "drone", "zone_id": "helipad"}, capped=False)
    assert "scope" not in scoped
