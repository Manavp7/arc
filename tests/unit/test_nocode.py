"""Workflows authored as JSON (PRD M22, Phase 6).

Validation is the entire product here. Parsing JSON into steps is twenty lines; the value is refusing a workflow
that would fail at 3am during a fire, and saying exactly why. A builder that accepts a workflow referencing an
activity that does not exist has not saved anybody a deploy — it has moved the failure from a code review to an
incident.

So most of this file is about what gets refused, and whether the refusal is useful.
"""

from __future__ import annotations

import json

import pytest
from sio_workflow.activities import ACTIVITIES
from sio_workflow.nocode import (
    MAX_STEPS,
    OPERATORS,
    TOP_LEVEL_FIELDS,
    Condition,
    load_workflows,
    parse,
    to_playbook,
    validate,
)
from sio_workflow.playbooks import PLAYBOOKS


def a_spec(**overrides):
    document = {
        "name": "TestFlow",
        "trigger": {"event_types": ["spill_detected"], "min_severity": "high", "cooldown_s": 600},
        "steps": [{"id": "notify", "activity": "notify_security"}],
    }
    document.update(overrides)
    return parse(document)


def problems_for(**overrides) -> list[str]:
    return [problem.where for problem in validate(a_spec(**overrides))]


# --- a valid workflow passes ----------------------------------------------------------------------
def test_a_valid_workflow_has_no_problems() -> None:
    assert validate(a_spec()) == []


def test_a_valid_workflow_becomes_a_playbook() -> None:
    """One engine, not two.

    A separate execution path for authored workflows would mean retries, compensation, cooldowns and run records
    had a second implementation that drifts — and the authored one would be the less-tested of the pair while
    dispatching the same drones.
    """
    playbook = to_playbook(a_spec())
    assert playbook.name == "TestFlow"
    assert playbook.trigger_event_types == ("spill_detected",)
    assert playbook.cooldown_s == 600
    assert playbook.steps[0].activity == "notify_security"
    # The same frozen type the Python playbooks are, so nothing downstream can tell them apart.
    assert type(playbook) is type(next(iter(PLAYBOOKS.values())))


def test_an_invalid_workflow_cannot_be_translated() -> None:
    """Refusing to translate, rather than translating something broken and failing at run time."""
    with pytest.raises(ValueError, match="invalid"):
        to_playbook(a_spec(steps=[{"id": "x", "activity": "no_such_activity"}]))


# --- the DAG --------------------------------------------------------------------------------------
def test_steps_run_in_dependency_order_not_authoring_order() -> None:
    """`after` is what makes it a DAG rather than a list.

    The engine runs steps in sequence and compensates in reverse, so the graph is topologically sorted into that
    order.
    """
    spec = a_spec(
        steps=[
            {"id": "record", "activity": "create_incident", "after": ["contain"]},
            {"id": "contain", "activity": "close_gate", "after": ["notify"]},
            {"id": "notify", "activity": "notify_security"},
        ]
    )
    assert [step.step_id for step in to_playbook(spec).steps] == ["notify", "contain", "record"]


def test_independent_steps_keep_their_authoring_order() -> None:
    """A stable tie-break, which matters for a UI.

    Two steps with no dependency between them should run in the order they appear on screen. An unstable sort
    would make the same workflow run differently between saves, and the author would have no way to explain it.
    """
    spec = a_spec(
        steps=[
            {"id": "one", "activity": "notify_security"},
            {"id": "two", "activity": "create_incident"},
            {"id": "three", "activity": "generate_report"},
        ]
    )
    assert [step.step_id for step in to_playbook(spec).steps] == ["one", "two", "three"]


def test_a_cycle_is_refused_with_the_path() -> None:
    """The path, not a boolean.

    "There is a cycle" leaves the author to find it in a graph they cannot see. And the reason it is refused is
    not graph-theory pedantry: compensation runs in reverse order, and a cycle has no order to reverse. A partial
    rollback is worse than none, because nobody can then tell what state the site is in.
    """
    spec = a_spec(
        steps=[
            {"id": "a", "activity": "close_gate", "after": ["c"]},
            {"id": "b", "activity": "open_gate", "after": ["a"]},
            {"id": "c", "activity": "notify_security", "after": ["b"]},
        ]
    )
    problems = validate(spec)
    assert problems, "a cycle must be refused"
    message = problems[0].message
    assert "→" in message, f"the cycle should be shown as a path, got {message!r}"
    assert "reverse" in (problems[0].fix or ""), "the reason involves compensation order"


def test_a_dependency_on_a_missing_step_is_refused_and_lists_what_exists() -> None:
    problems = validate(
        a_spec(
            steps=[
                {"id": "a", "activity": "close_gate"},
                {"id": "b", "activity": "open_gate", "after": ["typo"]},
            ]
        )
    )
    assert any("typo" in problem.message for problem in problems)
    assert any("'a'" in (problem.fix or "") for problem in problems)


def test_a_cycle_does_not_hang_the_sort() -> None:
    """`_topological_order` must terminate on a graph it cannot linearise."""
    spec = a_spec(
        steps=[
            {"id": "a", "activity": "close_gate", "after": ["b"]},
            {"id": "b", "activity": "open_gate", "after": ["a"]},
        ]
    )
    with pytest.raises(ValueError):
        to_playbook(spec)


# --- everything at once ---------------------------------------------------------------------------
def test_every_problem_is_reported_not_just_the_first() -> None:
    """A validator revealing one problem per attempt turns a five-minute task into twenty."""
    spec = parse(
        {
            "name": "bad name!",
            "trigger": {"event_types": [], "min_severity": "extremely", "cooldown_s": 0},
            "conditions": [{"field": "payload.zone", "op": "amongst", "value": "x"}],
            "steps": [
                {"id": "a", "activity": "launch_missile"},
                {"id": "a", "activity": "close_gate", "compensate": "unclose_gate"},
            ],
        }
    )
    problems = validate(spec)
    places = {problem.where for problem in problems}
    assert len(problems) >= 6, f"only found {len(problems)}: {places}"
    assert "name" in places
    assert "trigger.event_types" in places
    assert "trigger.min_severity" in places
    assert "conditions[0].op" in places
    assert "steps[0].activity" in places
    assert "steps[1].id" in places


def test_every_problem_names_a_place_a_ui_can_highlight() -> None:
    problems = validate(parse({"steps": [{"id": "a", "activity": "nope"}]}))
    assert problems
    for problem in problems:
        assert problem.where, "a problem with no location cannot be shown next to the field"


def test_an_unknown_activity_lists_the_available_ones() -> None:
    """Naming the options rather than saying "invalid" — the lesson the copilot's tools taught.

    An error that lists what is valid is one nobody has to ask about.
    """
    problems = validate(a_spec(steps=[{"id": "x", "activity": "launch_missile"}]))
    fix = next(problem.fix or "" for problem in problems if "activity" in problem.where)
    for activity in ACTIVITIES:
        assert activity in fix, f"{activity} is runnable but not offered in the error"


def test_an_unknown_operator_lists_the_available_ones() -> None:
    problems = validate(a_spec(conditions=[{"field": "payload.x", "op": "amongst", "value": 1}]))
    fix = next(problem.fix or "" for problem in problems if "op" in problem.where)
    for operator in OPERATORS:
        assert operator in fix


# --- refusals that matter -------------------------------------------------------------------------
def test_a_workflow_with_no_trigger_is_refused() -> None:
    """It would never run, and nothing would say so."""
    assert "trigger.event_types" in problems_for(trigger={"event_types": [], "cooldown_s": 60})


def test_a_workflow_with_no_steps_is_refused() -> None:
    assert "steps" in problems_for(steps=[])


def test_no_cooldown_is_a_problem_not_a_warning() -> None:
    """A fire produces `fire_detected` on nearly every frame while it burns.

    Without a cooldown that is a run per frame, each dispatching the same drone — and the consequence is
    invisible in testing, where events arrive one at a time.
    """
    assert "trigger.cooldown_s" in problems_for(
        trigger={"event_types": ["fire_detected"], "cooldown_s": 0}
    )


def test_too_many_steps_is_refused() -> None:
    """A no-code editor with no limit eventually receives a generated file with four thousand steps.

    The failure then looks like the workflow engine being broken.
    """
    steps = [{"id": f"s{index}", "activity": "notify_security"} for index in range(MAX_STEPS + 1)]
    assert "steps" in problems_for(steps=steps)


def test_a_step_cannot_be_both_compensable_and_irreversible() -> None:
    """ "Nothing to undo" and "cannot be undone" are different facts."""
    problems = problems_for(
        steps=[
            {
                "id": "a",
                "activity": "close_gate",
                "compensate": "open_gate",
                "irreversible": True,
            }
        ]
    )
    assert "steps[0]" in problems


def test_a_missing_compensation_activity_is_refused() -> None:
    """Discovering the rollback does not exist while rolling back is the worst possible moment."""
    problems = problems_for(steps=[{"id": "a", "activity": "close_gate", "compensate": "unclose"}])
    assert "steps[0].compensate" in problems


def test_in_without_a_list_is_refused() -> None:
    assert "conditions[0].value" in problems_for(
        conditions=[{"field": "payload.zone", "op": "in", "value": "fuel_store"}]
    )


# --- the hint that was wrong about itself ---------------------------------------------------------
def test_a_legitimate_top_level_field_is_not_flagged() -> None:
    """The mistake this validator made about its own suggestions.

    The first version flagged EVERY dotless field, so `zone_id` — which `/workflow/vocabulary` advertises —
    came back as a problem. A validator that complains about its own suggestions is one people learn to ignore,
    which costs far more than the hint is worth.
    """
    for field in TOP_LEVEL_FIELDS:
        assert validate(a_spec(conditions=[{"field": field, "op": "exists"}])) == [], (
            f"{field} is advertised as valid but the validator objects to it"
        )


def test_an_unknown_bare_field_still_gets_the_payload_hint() -> None:
    """The hint exists because the example plugin's rule was silently broken by exactly this.

    Sensor values live under `payload`, and a bare name matches nothing — quietly.
    """
    problems = validate(a_spec(conditions=[{"field": "water_level_m", "op": "gt", "value": 3}]))
    assert problems
    assert "payload.water_level_m" in (problems[0].fix or "")


# --- condition evaluation -------------------------------------------------------------------------
def test_a_missing_field_fails_every_comparison_except_exists() -> None:
    """Treating absence as zero makes `payload.count lt 5` true for an event with no count.

    That is how a workflow fires on something it was never meant to see.
    """
    fact: dict = {"payload": {}}
    assert not Condition("payload.count", "lt", 5).evaluate(fact)
    assert not Condition("payload.count", "gt", 5).evaluate(fact)
    assert not Condition("payload.count", "eq", 0).evaluate(fact)
    assert not Condition("payload.count", "exists").evaluate(fact)
    assert Condition("payload.count", "exists").evaluate({"payload": {"count": 0}})


def test_an_incompatible_comparison_is_false_not_a_crash() -> None:
    """The author made a mistake, and the right response is "did not match" plus a log line.

    Not tearing down the consumer for every other workflow.
    """
    fact = {"payload": {"zone_id": "fuel_store"}}
    assert not Condition("payload.zone_id", "gt", 5).evaluate(fact)


def test_conditions_reach_into_nested_payloads() -> None:
    fact = {"payload": {"zone_id": "fuel_store", "reading": {"value": 61.2}}}
    assert Condition("payload.zone_id", "eq", "fuel_store").evaluate(fact)
    assert Condition("payload.reading.value", "gt", 60).evaluate(fact)
    assert not Condition("payload.reading.value", "gt", 70).evaluate(fact)


def test_all_conditions_must_pass() -> None:
    spec = a_spec(
        conditions=[
            {"field": "payload.zone_id", "op": "in", "value": ["fuel_store"]},
            {"field": "payload.confidence", "op": "gte", "value": 0.8},
        ]
    )
    assert spec.matches({"payload": {"zone_id": "fuel_store", "confidence": 0.9}})
    assert not spec.matches({"payload": {"zone_id": "fuel_store", "confidence": 0.5}})
    assert not spec.matches({"payload": {"zone_id": "dock_1", "confidence": 0.9}})


def test_a_workflow_with_no_conditions_matches_everything_its_trigger_allows() -> None:
    """The trigger is the filter; conditions are an extra guard, not a required one."""
    assert a_spec().matches({"payload": {}})


def test_conditions_are_a_fixed_vocabulary_not_an_expression() -> None:
    """The moment an expression string exists somebody puts `count > 3 and zone in zones` in it.

    Then the platform has an interpreter, a sandbox problem and an injection surface — for a feature whose entire
    point was to avoid writing code.
    """
    from pathlib import Path

    from sio_workflow import nocode

    text = Path(nocode.__file__).read_text()
    assert "eval(" not in text
    assert "exec(" not in text


# --- loading from disk ----------------------------------------------------------------------------
def test_loading_reports_both_the_valid_and_the_rejected(tmp_path) -> None:
    """A workflow that failed to load is indistinguishable from one that never fires.

    The author will assume the latter. So both travel together, and a service that either refused to start over
    one bad file or silently ignored it would be wrong in different ways.
    """
    (tmp_path / "good.json").write_text(
        json.dumps(
            {
                "name": "Good",
                "trigger": {"event_types": ["spill_detected"], "cooldown_s": 300},
                "steps": [{"id": "a", "activity": "notify_security"}],
            }
        )
    )
    (tmp_path / "bad.json").write_text(
        json.dumps({"name": "Bad", "trigger": {"event_types": []}, "steps": []})
    )
    (tmp_path / "broken.json").write_text("{not json,}")

    specs, problems = load_workflows(tmp_path)
    assert [spec.name for spec in specs] == ["Good"]
    assert any("broken.json" in problem.where for problem in problems)
    assert any("bad.json" in problem.where for problem in problems)


def test_loading_an_absent_directory_is_not_an_error(tmp_path) -> None:
    """A deployment with no authored workflows is the normal case."""
    specs, problems = load_workflows(tmp_path / "nope")
    assert specs == []
    assert problems == []


def test_a_malformed_json_file_names_the_likely_cause(tmp_path) -> None:
    (tmp_path / "x.json").write_text('{"name": "X",}')
    _, problems = load_workflows(tmp_path)
    assert problems
    assert "comma" in (problems[0].fix or ""), "the commonest JSON mistake is worth naming"
