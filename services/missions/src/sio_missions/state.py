"""The mission lifecycle (PRD M17, Phase 6).

A mission moves `draft → active → paused → completed | aborted`, and the transitions worth thinking about are the
ones that are *refused*.

The temptation is to store `state` as a string and let any caller write any value. That is not a state machine, it
is a text field, and its failure mode is a mission that reads `completed` while three objectives are open and a
drone is still committed to it — because somebody's UI sent the wrong value and nothing objected.

So transitions are explicit, and a refusal names both what is legal now and *why* the requested move is not.
"why" matters more than it looks: "cannot go from draft to completed" is a fact, but "a mission that never
started cannot be complete — start it first, or abort it" tells the caller which of the two things they meant.
"""

from __future__ import annotations

from dataclasses import dataclass

from sio_schemas import MissionState

#: What may follow what.
#:
#: `paused → draft` is deliberately absent. Un-starting a mission that has already committed resources and
#: written comms entries would make `started_ts` a lie, and the log would describe events that, per the state,
#: had not happened yet. Abort it and open a new one; a mission is a record, not a scratchpad.
TRANSITIONS: dict[MissionState, tuple[MissionState, ...]] = {
    MissionState.DRAFT: (MissionState.ACTIVE, MissionState.ABORTED),
    MissionState.ACTIVE: (MissionState.PAUSED, MissionState.COMPLETED, MissionState.ABORTED),
    MissionState.PAUSED: (MissionState.ACTIVE, MissionState.COMPLETED, MissionState.ABORTED),
    # Terminal. A completed mission that can be reopened is one whose `completed_ts` means nothing, and the
    # question "was this mission finished at 14:20?" stops having an answer.
    MissionState.COMPLETED: (),
    MissionState.ABORTED: (),
}

#: States in which a mission holds its resources.
#:
#: A paused mission still holds them, and that is the point of pause existing at all: the drone is still yours
#: while you work out what to do next. If pause released resources it would be indistinguishable from abort with
#: extra steps.
HOLDS_RESOURCES = (MissionState.ACTIVE, MissionState.PAUSED)

#: States from which nothing more happens.
TERMINAL = (MissionState.COMPLETED, MissionState.ABORTED)


@dataclass(frozen=True)
class Refusal:
    """Why a transition was refused, and what to do instead."""

    message: str
    fix: str
    legal: tuple[str, ...]


def check(current: MissionState, requested: MissionState) -> Refusal | None:
    """`None` if the transition is allowed, otherwise why not.

    Returning a refusal rather than raising, so the caller decides whether it is a 409, a log line or a
    validation message next to a button. A state machine that raises forces every caller to catch.
    """
    if current == requested:
        return Refusal(
            message=f"the mission is already {current}",
            fix="nothing to do",
            legal=tuple(str(state) for state in TRANSITIONS[current]),
        )

    allowed = TRANSITIONS.get(current, ())
    if requested in allowed:
        return None

    if current in TERMINAL:
        return Refusal(
            message=f"{current} is final; a mission cannot leave it",
            # Said explicitly, because the alternative somebody reaches for is editing the row.
            fix=(
                f"a {current} mission is a record of what happened. Open a new mission rather than "
                f"reopening this one — otherwise completed_ts stops meaning anything, and "
                f"'was this finished at 14:20?' has no answer"
            ),
            legal=(),
        )

    if current == MissionState.DRAFT and requested == MissionState.COMPLETED:
        return Refusal(
            message="a mission that never started cannot be complete",
            fix="start it first, or abort it if it should not run",
            legal=tuple(str(state) for state in allowed),
        )

    if current == MissionState.DRAFT and requested == MissionState.PAUSED:
        return Refusal(
            message="a draft is not running, so there is nothing to pause",
            fix="a draft that should not run yet is simply left as a draft",
            legal=tuple(str(state) for state in allowed),
        )

    if requested == MissionState.DRAFT and current in (MissionState.ACTIVE, MissionState.PAUSED):
        return Refusal(
            message=f"a mission cannot be un-started, and this one is {current}",
            fix=(
                "it has already committed resources and written comms entries; making started_ts null "
                "would leave the log describing things that, per the state, had not happened. Abort it "
                "and open a new one"
            ),
            legal=tuple(str(state) for state in allowed),
        )

    return Refusal(
        message=f"a mission cannot go from {current} to {requested}",
        fix=f"legal moves from {current}: {', '.join(str(state) for state in allowed) or 'none'}",
        legal=tuple(str(state) for state in allowed),
    )


def completion_blockers(objectives: list[dict], *, force: bool = False) -> list[str]:
    """Reasons this mission should not be marked complete yet.

    Blockers rather than a hard refusal, and `force` exists, because the platform does not get to tell a
    commander that an operation is unfinished. Sometimes an objective becomes irrelevant — the truck left, the
    fire went out, the thing you were going to inspect burned down — and requiring somebody to tick a box that is
    no longer true, to close a mission that is plainly over, teaches them to tick boxes.

    What the platform *can* insist on is that the override is deliberate and recorded. `force` writes a comms
    entry naming what was outstanding, so the review afterwards sees the decision rather than a tidy mission.
    """
    if force:
        return []
    return [
        str(objective.get("description") or objective.get("objective_id"))
        for objective in objectives
        if not objective.get("done")
    ]


__all__ = [
    "HOLDS_RESOURCES",
    "TERMINAL",
    "TRANSITIONS",
    "Refusal",
    "check",
    "completion_blockers",
]
