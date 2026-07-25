"""Objective progress, derived rather than typed in (PRD M17, Phase 6).

This is the part that makes mission control belong in a spatial intelligence platform rather than in a task
tracker. A mission whose objectives are ticked by hand is a to-do list with a `zone_id` column; the platform
already knows whether a drone reached the fuel store, and asking a commander to tell it what it can see is both
insulting and unreliable — under load, the box that gets ticked is the one somebody remembers.

So an objective with a `zone_id` **completes itself** when an assigned resource is observed in that zone. Three
properties of that, each learned from something elsewhere in this codebase:

**Only assigned resources count.** A forklift wandering through the fuel store does not satisfy "get eyes on the
fuel store" — the objective is about the mission's own resources doing the thing. Without this, a busy yard
completes objectives by accident, which is worse than not completing them at all because it looks like success.

**Completion is sticky.** Once satisfied, an objective stays done even when the drone leaves. The requirement was
"get eyes on it", not "keep eyes on it for ever", and an objective that un-completes when a drone moves on
produces a progress bar that goes backwards — which nobody trusts a second time.

**Progress is computed, never stored.** A stored percentage drifts from the objectives it summarises the moment
one is added, and then two numbers on one screen disagree. The bar is a function of the list, always.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Progress:
    """How far along a mission is, and what it is waiting for."""

    done: int
    total: int
    fraction: float
    outstanding: tuple[str, ...]
    #: Objectives that no resource can currently satisfy, because nothing is assigned that could.
    #:
    #: Surfaced separately from `outstanding` because they are a different problem: an outstanding objective is
    #: work in progress, an unreachable one is work nobody is doing. A progress bar cannot distinguish them and
    #: a commander needs to.
    unassigned: tuple[str, ...] = ()

    @property
    def percent(self) -> int:
        return round(self.fraction * 100)

    def describe(self) -> dict[str, Any]:
        return {
            "done": self.done,
            "total": self.total,
            "fraction": round(self.fraction, 3),
            "percent": self.percent,
            "outstanding": list(self.outstanding),
            "unassigned": list(self.unassigned),
            "summary": self.summary,
        }

    @property
    def summary(self) -> str:
        """One sentence, because a percentage alone does not tell anybody what to do.

        `60%` prompts "which 40%?" every single time. Naming what is outstanding answers the question the number
        raises, which is the whole point of the number.
        """
        if self.total == 0:
            return "no objectives set, so there is nothing to measure"
        if self.done == self.total:
            return f"all {self.total} objectives met"
        waiting = ", ".join(self.outstanding[:2])
        more = f" and {len(self.outstanding) - 2} more" if len(self.outstanding) > 2 else ""
        sentence = f"{self.done} of {self.total} met; waiting on {waiting}{more}"
        if self.unassigned:
            sentence += (
                f". {len(self.unassigned)} objective(s) have no assigned resource that could "
                f"satisfy them"
            )
        return sentence


def evaluate(
    objectives: list[dict[str, Any]],
    *,
    occupancy: dict[str, set[str]] | None = None,
    resources: tuple[str, ...] = (),
) -> tuple[list[dict[str, Any]], Progress]:
    """Auto-complete what the world has already satisfied, and measure the rest.

    `occupancy` maps a zone id to the entity ids currently in it — the world model's answer, not a guess. Passing
    it in rather than querying here keeps this function pure and testable, which matters because "why did that
    objective complete?" is a question somebody will ask about a real incident.

    Returns the objectives (possibly updated) and the progress. Both, because the caller needs to persist the
    first and serve the second, and computing progress from stale objectives is the bug this signature prevents.
    """
    occupancy = occupancy or {}
    assigned = set(resources)
    updated: list[dict[str, Any]] = []
    outstanding: list[str] = []
    unassigned: list[str] = []

    for objective in objectives:
        current = dict(objective)
        label = str(current.get("description") or current.get("objective_id") or "an objective")

        if not current.get("done"):
            zone = current.get("zone_id")
            if zone:
                present = occupancy.get(str(zone), set())
                # Only the mission's own resources. A forklift passing through does not satisfy "get eyes on the
                # fuel store", and a busy yard would otherwise complete objectives by accident — which looks like
                # success and is not.
                satisfying = present & assigned
                if satisfying:
                    current["done"] = True
                    current["progress"] = 1.0
                    # Recorded so the answer to "why did that complete?" survives the moment.
                    current["satisfied_by"] = sorted(satisfying)
                elif not assigned:
                    unassigned.append(label)
                    outstanding.append(label)
                else:
                    outstanding.append(label)
            else:
                # No zone: nothing to observe, so it is a human judgement and stays manual. Being explicit about
                # which objectives the platform can and cannot verify is more useful than pretending uniformity.
                outstanding.append(label)
        else:
            current.setdefault("progress", 1.0)

        updated.append(current)

    total = len(updated)
    done = sum(1 for objective in updated if objective.get("done"))
    # A mission with no objectives reads 0%, not 100%. "Nothing required, therefore complete" is technically
    # defensible and operationally awful: a mission somebody has not finished writing would show as finished.
    fraction = (done / total) if total else 0.0
    return updated, Progress(
        done=done,
        total=total,
        fraction=fraction,
        outstanding=tuple(outstanding),
        unassigned=tuple(unassigned),
    )


def newly_completed(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Objectives that changed from open to done, so a comms entry can record each one.

    Diffing rather than having `evaluate` report it, because the caller may not have persisted `before` — and an
    objective completing is exactly the kind of thing that should appear in the log without anybody typing it.
    """
    was_open = {
        str(objective.get("objective_id")) for objective in before if not objective.get("done")
    }
    return [
        objective
        for objective in after
        if objective.get("done") and str(objective.get("objective_id")) in was_open
    ]


__all__ = ["Progress", "evaluate", "newly_completed"]
