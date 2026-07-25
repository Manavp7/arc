"""Copilot question-answering accuracy (PRD §16, Phase 8).

Scores the copilot against the 25 fixed questions in `services/copilot/src/sio_copilot/evalset.py` — the same
fixture `scripts/eval_tool_calling.py` uses to compare models. Reusing it rather than writing a second set is
the point: two eval sets drift, and then two numbers disagree about whether the copilot got better.

**Three metrics, because "accuracy" hides the failure that matters.**

* **selection** — did it choose the right tool? The headline, and the one a demo depends on.
* **arguments** — were the required arguments right? A correct tool with the wrong zone answers a different
  question, fluently, which is worse than refusing. Scored only over cases whose tool was already correct, so
  one mistake is not punished twice.
* **restraint** — did it stay quiet when it should have? Three of the cases are questions a copilot should
  answer *without* reaching for the world model, and a model that calls `list_entities` in response to "hello"
  is one nobody trusts with a tool belt.

**The default run scores `ScriptedLLM`, not Ollama**, and that is deliberate. This ring has to be runnable on a
laptop with no model server, in seconds, and produce the same number twice — a metric that needs a 3B model and
30 seconds is one that gets skipped. Scripted routing exercises the fixture, the tool registry and the argument
extraction; what it cannot exercise is whether a real model chooses well, which is `just eval-tools`' job
against a live Ollama. Both are useful and neither substitutes for the other, so the scorecard says which ran.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.eval


def _score_cases(reply_for) -> tuple[float, float, float, int]:  # type: ignore[no-untyped-def]
    """Selection, arguments and restraint over the fixture.

    `reply_for` takes a question and returns the tool call the model made, or None. Injected rather than fixed
    so the same scoring runs against a scripted model here and a real one in the script.
    """
    from sio_copilot.evalset import EVAL_CASES

    action_cases = [case for case in EVAL_CASES if case.expect_tool is not None]
    restraint_cases = [case for case in EVAL_CASES if case.expect_tool is None]

    selected_right = 0
    argument_right = 0
    argument_scored = 0
    stayed_quiet = 0

    for case in action_cases:
        call = reply_for(case.question)
        chosen = call.name if call else None
        acceptable = {case.expect_tool, *case.acceptable_tools}
        if chosen in acceptable:
            selected_right += 1
            if case.expect_arguments:
                argument_scored += 1
                supplied = call.arguments if call else {}
                if all(supplied.get(key) == value for key, value in case.expect_arguments.items()):
                    argument_right += 1

    for case in restraint_cases:
        if reply_for(case.question) is None:
            stayed_quiet += 1

    selection = selected_right / len(action_cases) if action_cases else 0.0
    arguments = argument_right / argument_scored if argument_scored else 1.0
    restraint = stayed_quiet / len(restraint_cases) if restraint_cases else 1.0
    return selection, arguments, restraint, len(EVAL_CASES)


def test_copilot_tool_selection(scorecard) -> None:  # type: ignore[no-untyped-def]
    """The copilot's accuracy on the fixed question set.

    Floors are high because this is the SCRIPTED router: it should get essentially everything right, and
    anything below the floor means the fixture and the routes have diverged — a tool renamed, a question added
    without a route, an argument key changed. That is a real regression and exactly what this catches cheaply.
    """
    try:
        from sio_copilot.evalset import scripted_routes

        from sio_core.llm.scripted import ScriptedLLM
    except ImportError as error:  # pragma: no cover
        for name, floor in (
            ("copilot tool selection", 0.85),
            ("copilot argument accuracy", 0.85),
            ("copilot restraint", 0.90),
        ):
            scorecard.skip(name, floor=floor, reason=f"copilot unavailable: {error}")
        pytest.skip("copilot is not installed")

    import asyncio

    model = ScriptedLLM(routes=scripted_routes())

    def reply_for(question: str):  # type: ignore[no-untyped-def]
        reply = asyncio.run(model.chat([{"role": "user", "content": question}]))
        return reply.tool_calls[0] if reply.tool_calls else None

    selection, arguments, restraint, total = _score_cases(reply_for)

    recorded = scorecard.record(
        "copilot tool selection",
        selection,
        floor=0.85,
        detail=f"{total} fixed questions, scripted router",
    )
    scorecard.record(
        "copilot argument accuracy",
        arguments,
        floor=0.85,
        # Named, because "accuracy" without this is the metric people quote and the one that hides the failure:
        # a right tool with a wrong zone answers a different question fluently.
        detail="required arguments correct, over cases whose tool was right",
    )
    scorecard.record(
        "copilot restraint",
        restraint,
        floor=0.90,
        detail="stayed quiet on questions that need no tool",
    )

    assert recorded.passed, (
        f"tool selection {selection:.2f} is below 0.85 on the scripted router. That is not a model problem — "
        f"the scripted routes and the eval fixture have diverged. Check for a renamed tool or a question added "
        f"to EVAL_CASES without a matching route."
    )


def test_the_fixture_covers_restraint_and_arguments(scorecard) -> None:  # type: ignore[no-untyped-def]
    """The fixture's own shape, asserted.

    An eval set of 25 questions that all expect a tool call would score a model that never stays quiet at 100%,
    and an eval set with no required arguments would score one that calls the right tool with empty arguments at
    100%. The fixture has to contain the cases that can fail, or the metric is decoration.
    """
    from sio_copilot.evalset import EVAL_CASES

    restraint = [case for case in EVAL_CASES if case.expect_tool is None]
    with_arguments = [case for case in EVAL_CASES if case.expect_arguments]

    assert len(EVAL_CASES) >= 20, "the plan calls for at least 20 fixed questions"
    assert len(restraint) >= 3, (
        "without restraint cases, a model that calls a tool for every input scores perfectly"
    )
    assert len(with_arguments) >= 5, (
        "without argument expectations, a model that calls the right tool with empty arguments scores perfectly"
    )
    # Recorded so the scorecard shows what the number was measured over — a metric whose denominator is
    # invisible is one people compare across incompatible runs.
    scorecard.record(
        "copilot fixture size",
        float(len(EVAL_CASES)),
        floor=20.0,
        unit="questions",
        detail=f"{len(restraint)} restraint, {len(with_arguments)} with required arguments",
    )


def test_every_expected_tool_exists(scorecard) -> None:  # type: ignore[no-untyped-def]
    """A fixture that expects a tool nobody implements is unwinnable.

    Worth checking mechanically because it fails silently in the other direction: the model is marked wrong on
    every attempt, the score drops, and the obvious conclusion is that the model got worse.
    """
    from sio_copilot.evalset import EVAL_CASES

    try:
        from sio_copilot.tools import ToolBelt

        available = {spec.name for spec in ToolBelt.specs()}
    except Exception as error:  # pragma: no cover - the belt needs settings in some configurations
        pytest.skip(f"tool belt unavailable: {error}")

    expected = {case.expect_tool for case in EVAL_CASES if case.expect_tool}
    expected |= {tool for case in EVAL_CASES for tool in case.acceptable_tools}
    missing = sorted(expected - available)
    assert not missing, (
        f"the eval fixture expects tools that do not exist: {missing}. Every attempt at these cases is "
        f"scored wrong, which looks like the model degrading."
    )
