"""Mission control (PRD M17, Phase 6).

A mission is the one object in this platform a *human* owns, and that shapes what is worth testing. Most services
are tested on whether they compute the right answer; this one is tested on whether it refuses the right things and
explains itself when it does.

The two behaviours with real consequences:

* **objectives complete themselves from observation**, and only from *assigned* resources — otherwise a busy yard
  completes objectives by accident, which looks like success;
* **a resource cannot be committed to two missions**, because dispatching the same drone to two fires is the
  failure this service exists to prevent.
"""

from __future__ import annotations

import pytest
from sio_missions.progress import Progress, evaluate, newly_completed
from sio_missions.state import (
    HOLDS_RESOURCES,
    TERMINAL,
    TRANSITIONS,
    check,
    completion_blockers,
)

from sio_schemas import MissionState as S


def objectives() -> list[dict]:
    return [
        {
            "objective_id": "o1",
            "description": "Get eyes on the fuel store",
            "zone_id": "fuel_store",
        },
        {"objective_id": "o2", "description": "Check dock 3", "zone_id": "dock_3"},
        {"objective_id": "o3", "description": "Confirm the area is safe"},
    ]


# --- the state machine ----------------------------------------------------------------------------
def test_the_happy_path_is_allowed() -> None:
    assert check(S.DRAFT, S.ACTIVE) is None
    assert check(S.ACTIVE, S.COMPLETED) is None
    assert check(S.ACTIVE, S.PAUSED) is None
    assert check(S.PAUSED, S.ACTIVE) is None


def test_anything_can_be_aborted_except_a_finished_mission() -> None:
    """Abort is the escape hatch, and it must always be reachable from a live mission."""
    for state in (S.DRAFT, S.ACTIVE, S.PAUSED):
        assert check(state, S.ABORTED) is None
    assert check(S.COMPLETED, S.ABORTED) is not None


@pytest.mark.parametrize("state", TERMINAL)
def test_a_terminal_state_is_terminal(state: S) -> None:
    """A completed mission that can be reopened is one whose `completed_ts` means nothing.

    "Was this finished at 14:20?" then stops having an answer, which is the question an incident review asks
    first.
    """
    assert TRANSITIONS[state] == ()
    for target in S:
        if target == state:
            continue
        refusal = check(state, target)
        assert refusal is not None
        assert "final" in refusal.message
        # The fix names the alternative, because the thing somebody reaches for instead is editing the row.
        assert "new mission" in refusal.fix


def test_a_draft_cannot_be_completed_and_the_refusal_says_what_to_do() -> None:
    """ "Cannot go from draft to completed" is a fact; this tells the caller which thing they meant."""
    refusal = check(S.DRAFT, S.COMPLETED)
    assert refusal is not None
    assert "never started" in refusal.message
    assert "abort" in refusal.fix
    assert set(refusal.legal) == {"active", "aborted"}


def test_a_mission_cannot_be_un_started_from_either_running_state() -> None:
    """Un-starting a mission that has committed resources would make `started_ts` a lie.

    The log would then describe events that, per the state, had not happened yet. Both `active` and `paused`
    give the same explanation, because it is the same fact — the first version only special-cased `paused` and
    left `active` with a generic message.
    """
    for state in (S.ACTIVE, S.PAUSED):
        refusal = check(state, S.DRAFT)
        assert refusal is not None
        assert "un-started" in refusal.message
        assert str(state) in refusal.message


def test_a_draft_cannot_be_paused() -> None:
    refusal = check(S.DRAFT, S.PAUSED)
    assert refusal is not None
    assert "nothing to pause" in refusal.message


def test_moving_to_the_state_you_are_already_in_is_reported_not_silently_allowed() -> None:
    """Silently succeeding would write a second `started_ts` and a duplicate comms entry."""
    refusal = check(S.ACTIVE, S.ACTIVE)
    assert refusal is not None
    assert "already" in refusal.message


def test_every_refusal_names_the_legal_moves_or_says_there_are_none() -> None:
    for current in S:
        for requested in S:
            refusal = check(current, requested)
            if refusal is None:
                continue
            expected = tuple(str(state) for state in TRANSITIONS.get(current, ()))
            # A refusal that does not say what IS possible makes the caller guess.
            assert refusal.legal == expected or refusal.legal == ()
            assert refusal.fix, f"{current} → {requested} refused without a fix"


def test_a_paused_mission_keeps_its_resources() -> None:
    """That is the entire point of pause existing.

    The drone is still yours while you work out what to do next. If pause released resources it would be abort
    with extra steps.
    """
    assert S.PAUSED in HOLDS_RESOURCES
    assert S.ACTIVE in HOLDS_RESOURCES
    assert S.COMPLETED not in HOLDS_RESOURCES
    assert S.ABORTED not in HOLDS_RESOURCES
    # A draft has not committed anything yet either.
    assert S.DRAFT not in HOLDS_RESOURCES


# --- objectives that complete themselves ----------------------------------------------------------
def test_an_assigned_resource_in_the_zone_completes_the_objective() -> None:
    updated, progress = evaluate(
        objectives(), occupancy={"fuel_store": {"drone-7"}}, resources=("drone-7",)
    )
    assert updated[0]["done"] is True
    assert updated[0]["satisfied_by"] == ["drone-7"]
    assert progress.done == 1
    assert progress.percent == 33


def test_an_unassigned_entity_in_the_zone_does_not() -> None:
    """A forklift wandering through does not satisfy "get eyes on the fuel store".

    Without this a busy yard completes objectives by accident, which is worse than not completing them at all
    because it looks like success. In the live run six entities were in `lane_north` and the objective stayed
    open until an assigned one arrived.
    """
    updated, progress = evaluate(
        objectives(),
        occupancy={"fuel_store": {"forklift-2", "worker-9"}},
        resources=("drone-7",),
    )
    assert updated[0].get("done") is not True
    assert progress.done == 0


def test_completion_is_sticky_when_the_resource_leaves() -> None:
    """The requirement was "get eyes on it", not "keep eyes on it for ever".

    An objective that un-completes when a drone moves on produces a progress bar that goes backwards, and
    nobody trusts one of those a second time.
    """
    once, _ = evaluate(objectives(), occupancy={"fuel_store": {"drone-7"}}, resources=("drone-7",))
    assert once[0]["done"] is True
    later, progress = evaluate(once, occupancy={}, resources=("drone-7",))
    assert later[0]["done"] is True
    assert progress.done == 1


def test_an_objective_without_a_zone_is_never_auto_completed() -> None:
    """ "Confirm the area is safe" is a human judgement the platform cannot verify.

    Being explicit about which objectives it can and cannot check is more useful than pretending uniformity.
    """
    updated, _ = evaluate(
        objectives(),
        # Even with everything everywhere, the zone-less objective stays open.
        occupancy={"fuel_store": {"drone-7"}, "dock_3": {"drone-7"}},
        resources=("drone-7",),
    )
    assert updated[2].get("done") is not True


def test_objectives_with_nothing_assigned_are_reported_as_unreachable() -> None:
    """An outstanding objective is work in progress; an unreachable one is work NOBODY is doing.

    A progress bar cannot distinguish them and a commander needs to, so they are separate fields.
    """
    _, progress = evaluate(objectives(), occupancy={"fuel_store": {"drone-7"}}, resources=())
    assert len(progress.unassigned) == 2, "the two zone-based objectives have no assigned resource"
    assert "no assigned resource" in progress.summary


def test_a_mission_with_no_objectives_is_zero_percent_not_a_hundred() -> None:
    """ "Nothing required, therefore complete" is technically defensible and operationally awful.

    A mission somebody has not finished writing would show as finished.
    """
    _, progress = evaluate([])
    assert progress.percent == 0
    assert progress.total == 0
    assert "nothing to measure" in progress.summary


def test_progress_carries_a_sentence_not_just_a_number() -> None:
    """`60%` prompts "which 40%?" every single time.

    Naming what is outstanding answers the question the number raises, which is the point of the number.
    """
    _, progress = evaluate(
        objectives(), occupancy={"fuel_store": {"drone-7"}}, resources=("drone-7",)
    )
    assert "1 of 3" in progress.summary
    assert "Check dock 3" in progress.summary


def test_a_fully_met_mission_says_so_plainly() -> None:
    updated, progress = evaluate(
        objectives(),
        occupancy={"fuel_store": {"drone-7"}, "dock_3": {"drone-7"}},
        resources=("drone-7",),
    )
    updated[2]["done"] = True
    _, progress = evaluate(updated, occupancy={}, resources=("drone-7",))
    assert progress.percent == 100
    assert "all 3 objectives met" in progress.summary


def test_the_summary_does_not_list_every_outstanding_objective() -> None:
    """A one-line summary that grows without bound stops being a summary."""
    many = [
        {"objective_id": f"o{index}", "description": f"Objective {index}", "zone_id": "z"}
        for index in range(12)
    ]
    _, progress = evaluate(many, occupancy={}, resources=("drone-7",))
    assert "and 10 more" in progress.summary


def test_progress_is_derived_and_never_taken_from_the_input() -> None:
    """A stored percentage drifts from its objectives the moment one is added.

    Two numbers on one screen then disagree about the same mission. Passing a deliberately wrong `progress`
    through must not influence the result.
    """
    lying = [
        {"objective_id": "o1", "description": "one", "done": False, "progress": 1.0},
        {"objective_id": "o2", "description": "two", "done": False, "progress": 1.0},
    ]
    _, progress = evaluate(lying)
    assert progress.done == 0
    assert progress.percent == 0


def test_newly_completed_finds_only_the_transitions() -> None:
    """So a comms entry is written once, when it happens, and not on every tick afterwards."""
    before = objectives()
    after, _ = evaluate(before, occupancy={"fuel_store": {"drone-7"}}, resources=("drone-7",))
    assert [item["objective_id"] for item in newly_completed(before, after)] == ["o1"]
    # Second pass: nothing new, so nothing is logged again.
    again, _ = evaluate(after, occupancy={"fuel_store": {"drone-7"}}, resources=("drone-7",))
    assert newly_completed(after, again) == []


def test_the_satisfying_resource_is_recorded() -> None:
    """ "Objective met" without a cause makes an incident review harder rather than easier."""
    updated, _ = evaluate(
        objectives(),
        occupancy={"fuel_store": {"drone-7", "drone-8", "forklift-2"}},
        resources=("drone-7", "drone-8"),
    )
    assert updated[0]["satisfied_by"] == ["drone-7", "drone-8"]
    assert "forklift-2" not in updated[0]["satisfied_by"]


def test_evaluate_does_not_mutate_its_input() -> None:
    """The caller diffs before against after, so the input has to survive the call."""
    before = objectives()
    evaluate(before, occupancy={"fuel_store": {"drone-7"}}, resources=("drone-7",))
    assert before[0].get("done") is not True


# --- completion blockers --------------------------------------------------------------------------
def test_open_objectives_block_completion() -> None:
    assert len(completion_blockers(objectives())) == 3


def test_force_overrides_the_blockers() -> None:
    """The platform does not get to tell a commander that an operation is unfinished.

    Sometimes an objective becomes irrelevant — the truck left, the fire went out. Requiring somebody to tick a
    box that is no longer true, to close a mission that is plainly over, teaches them to tick boxes.
    """
    assert completion_blockers(objectives(), force=True) == []


def test_a_mission_whose_objectives_are_met_has_no_blockers() -> None:
    met = [{**item, "done": True} for item in objectives()]
    assert completion_blockers(met) == []


def test_blockers_are_described_not_just_counted() -> None:
    """The list goes into a comms entry, so it has to be readable by whoever reviews the mission."""
    blockers = completion_blockers(objectives())
    assert "Get eyes on the fuel store" in blockers


# --- the shape of Progress ------------------------------------------------------------------------
def test_progress_describes_itself_for_the_api() -> None:
    _, progress = evaluate(
        objectives(), occupancy={"fuel_store": {"drone-7"}}, resources=("drone-7",)
    )
    described = progress.describe()
    for key in ("done", "total", "fraction", "percent", "outstanding", "unassigned", "summary"):
        assert key in described, f"the console reads {key}"
    assert isinstance(described["percent"], int)


def test_a_progress_fraction_is_bounded() -> None:
    for count in (0, 1, 5):
        items = [
            {"objective_id": f"o{index}", "description": "x", "done": True}
            for index in range(count)
        ]
        _, progress = evaluate(items)
        assert 0.0 <= progress.fraction <= 1.0


def test_progress_is_immutable() -> None:
    """It is derived, so a caller mutating it would be writing to a cache of a computation."""
    progress = Progress(done=1, total=2, fraction=0.5, outstanding=("x",))
    with pytest.raises((AttributeError, TypeError)):
        progress.done = 2  # type: ignore[misc]
