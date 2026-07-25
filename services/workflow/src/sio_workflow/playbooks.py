"""Response playbooks as data (PRD M15).

A playbook is a list of steps with retry policies, timeouts and compensations. Keeping it as data rather
than as code has the same payoff as the events rule engine: a new playbook is a definition, and the
interesting behaviour — ordering, retries, undo — is implemented once and shared.

**Compensation is the part that matters, and the part usually missing.** A five-step fire response that
fails at step four has already dispatched a drone and closed a gate. "Failed" is not a state anyone can
act on: the gate is still shut, the drone is still airborne, and nobody knows. So every step that changes
something declares how to undo it, and a failed run reverses what it did in the order it did it.

Two decisions that follow from the same idea:

* **Some steps must not be undone.** Once security has been notified, un-notifying them is not a thing;
  the compensation is to notify them again that the response was aborted. Declaring `compensate=None`
  would silently do nothing, which reads as success — so the absence of an undo is explicit
  (`irreversible=True`) and it appears in the run's record.
* **Retries need idempotency, not just a count.** A step that times out may have already acted, so
  retrying it can double-dispatch. Each activity takes an `idempotency_key` derived from the run and step,
  and is responsible for using it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StepSpec:
    """One step of a playbook."""

    step_id: str
    name: str
    activity: str
    """Name of the activity to run, resolved by the activity registry."""
    arguments: dict[str, Any] = field(default_factory=dict)
    timeout_s: float = 20.0
    max_attempts: int = 3
    """Attempts including the first. Three is a compromise: enough to ride out a restart, few enough that
    a genuinely broken step fails while an operator is still watching."""
    backoff_s: float = 1.0
    compensate: str | None = None
    """Activity that undoes this step, run in reverse order when a later step fails."""
    irreversible: bool = False
    """True when the step cannot be undone, only acknowledged.

    Explicit rather than implied by `compensate=None`, because "nothing to undo" and "cannot be undone"
    are different facts and only one of them is safe to pass over in silence.
    """
    optional: bool = False
    """When true, a failure is recorded and the run continues.

    Used for steps whose value is informational — a report that fails to generate should not roll back a
    fire response that has already dispatched a drone.
    """

    def __post_init__(self) -> None:
        if self.compensate and self.irreversible:
            raise ValueError(f"step {self.step_id!r} cannot be both compensable and irreversible")


@dataclass(frozen=True)
class Playbook:
    """A named sequence of steps, with what triggers it."""

    name: str
    description: str
    steps: tuple[StepSpec, ...]
    trigger_event_types: tuple[str, ...] = ()
    """Event types that start this playbook."""
    trigger_min_severity: str = "high"
    cooldown_s: float = 300.0
    """Minimum gap between runs for the same subject.

    A fire produces `fire_detected` on nearly every frame while it burns. Without a cooldown a single fire
    starts a hundred playbooks, each dispatching the same drone — the exact failure the events engine's
    cooldowns exist to prevent, reappearing one layer up.
    """
    key_by: tuple[str, ...] = ("zone_id",)
    """What counts as the same subject for the cooldown."""

    @property
    def step_count(self) -> int:
        return len(self.steps)

    def step(self, step_id: str) -> StepSpec | None:
        return next((step for step in self.steps if step.step_id == step_id), None)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "triggers": list(self.trigger_event_types),
            "min_severity": self.trigger_min_severity,
            "cooldown_s": self.cooldown_s,
            "steps": [
                {
                    "step_id": step.step_id,
                    "name": step.name,
                    "activity": step.activity,
                    "timeout_s": step.timeout_s,
                    "max_attempts": step.max_attempts,
                    "compensate": step.compensate,
                    "irreversible": step.irreversible,
                    "optional": step.optional,
                }
                for step in self.steps
            ],
        }


FIRE_RESPONSE = Playbook(
    name="FireResponsePlaybook",
    description=(
        "The PRD's headline scenario: get eyes on the fire, tell a human, contain the area, record the "
        "incident, and produce a report."
    ),
    trigger_event_types=("fire_detected", "smoke_detected"),
    trigger_min_severity="high",
    cooldown_s=600.0,
    steps=(
        StepSpec(
            step_id="dispatch_drone",
            name="Dispatch the patrol drone for visual confirmation",
            activity="dispatch_drone",
            timeout_s=15.0,
            # First, because a fire detection from a heuristic detector needs confirming before anyone
            # closes a gate on the strength of it.
            compensate="recall_drone",
        ),
        StepSpec(
            step_id="notify_security",
            name="Notify the security team",
            activity="notify_security",
            timeout_s=10.0,
            # Cannot be undone. The compensation for an aborted response is another message, not an
            # un-message, and that is handled by the abort path rather than pretended away here.
            irreversible=True,
        ),
        StepSpec(
            step_id="close_gate",
            name="Close the affected gate",
            activity="close_gate",
            timeout_s=15.0,
            compensate="open_gate",
        ),
        StepSpec(
            step_id="create_incident",
            name="Open an incident record",
            activity="create_incident",
            timeout_s=10.0,
            compensate="close_incident",
        ),
        StepSpec(
            step_id="generate_report",
            name="Generate the incident report",
            activity="generate_report",
            timeout_s=20.0,
            # Optional: a report that fails to render must not roll back a response that has already
            # dispatched a drone and closed a gate.
            optional=True,
        ),
    ),
)

INTRUSION_RESPONSE = Playbook(
    name="IntrusionPlaybook",
    description="Someone is in a restricted zone: confirm visually, tell security, record it.",
    trigger_event_types=("unauthorized_entry", "zone_breach"),
    trigger_min_severity="high",
    cooldown_s=300.0,
    steps=(
        StepSpec(
            step_id="dispatch_drone",
            name="Send the drone to the zone",
            activity="dispatch_drone",
            compensate="recall_drone",
        ),
        StepSpec(
            step_id="notify_security",
            name="Notify the security team",
            activity="notify_security",
            irreversible=True,
        ),
        StepSpec(
            step_id="create_incident",
            name="Open an incident record",
            activity="create_incident",
            compensate="close_incident",
        ),
    ),
)

DWELL_ESCALATION = Playbook(
    name="DwellEscalationPlaybook",
    description="A vehicle has been at a dock too long: tell the yard team and record it for the report.",
    trigger_event_types=("dwell_exceeded",),
    trigger_min_severity="medium",
    cooldown_s=900.0,
    key_by=("entity_id",),
    steps=(
        StepSpec(
            step_id="notify_yard",
            name="Notify the yard team",
            activity="notify_security",
            arguments={"team": "yard"},
            irreversible=True,
        ),
        StepSpec(
            step_id="create_incident",
            name="Record the delay",
            activity="create_incident",
            arguments={"kind": "dwell"},
            compensate="close_incident",
        ),
        StepSpec(
            step_id="generate_report",
            name="Add it to the turnaround report",
            activity="generate_report",
            optional=True,
        ),
    ),
)

PLAYBOOKS: dict[str, Playbook] = {
    playbook.name: playbook for playbook in (FIRE_RESPONSE, INTRUSION_RESPONSE, DWELL_ESCALATION)
}


def playbooks_for(event_type: str, severity: str) -> list[Playbook]:
    """Which playbooks a given event should start.

    Severity is compared as a rank, not as a string: "high" is not greater than "critical"
    alphabetically, and a playbook that failed to fire on the most severe events would be a very quiet
    bug.
    """
    ranks = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    actual = ranks.get(severity, 0)
    return [
        playbook
        for playbook in PLAYBOOKS.values()
        if event_type in playbook.trigger_event_types
        and actual >= ranks.get(playbook.trigger_min_severity, 3)
    ]


__all__ = [
    "DWELL_ESCALATION",
    "FIRE_RESPONSE",
    "INTRUSION_RESPONSE",
    "PLAYBOOKS",
    "Playbook",
    "StepSpec",
    "playbooks_for",
]
