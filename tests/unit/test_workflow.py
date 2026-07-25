"""Tests for response playbooks (PRD M15).

The forward path is the easy half. What these tests concentrate on is what happens when a step fails
partway through a response that has already changed the world, because "failed" is not a state anyone can
act on if a gate is still shut and a drone is still airborne:

* completed steps are **undone in reverse order**;
* an **irreversible** step is acknowledged rather than silently passed over;
* a **failed compensation** escalates, because the world is then in a state nobody chose;
* an **optional** step's failure does not roll back a response that has already acted;
* a **retry** does not double-act, because a timeout means the step may already have run.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sio_workflow import (
    FIRE_RESPONSE,
    PLAYBOOKS,
    ActivityContext,
    InlineRunner,
    Playbook,
    RunLedger,
    StepSpec,
    playbooks_for,
)
from sio_workflow.activities import ACTIVITIES, idempotent

from sio_schemas import RunStatus


def a_context(**kwargs: Any) -> ActivityContext:
    defaults: dict[str, Any] = {
        "api_url": "http://fake",
        "ingest_url": "http://fake",
        "tenant_id": "acme",
        "run_id": "wfr_test",
        "zone_id": "dock_3",
        "dry_run": True,
    }
    defaults.update(kwargs)
    return ActivityContext(**defaults)


def fast(playbook: Playbook) -> Playbook:
    """The same playbook with a negligible retry backoff.

    The shipped backoff is a second, doubling — right for a service that might be restarting, and three
    seconds of dead time per failing step in a test suite. The structure under test is unchanged.
    """
    from dataclasses import replace

    return replace(
        playbook,
        steps=tuple(replace(step, backoff_s=0.01) for step in playbook.steps),
    )


def recording_activities(
    *, fail: set[str] | None = None, hang: set[str] | None = None, order: list[str] | None = None
) -> dict[str, Any]:
    """Activities that record the order they ran in, and fail or hang on demand."""
    fail = fail or set()
    hang = hang or set()
    log = order if order is not None else []

    def make(name: str) -> Any:
        async def activity(
            context: ActivityContext, step_id: str, **arguments: Any
        ) -> dict[str, Any]:
            log.append(name)
            if name in hang:
                await asyncio.sleep(30)
            if name in fail:
                raise RuntimeError(f"{name} refused")
            return {"activity": name, "step": step_id}

        return activity

    return {name: make(name) for name in ACTIVITIES}


# ------------------------------------------------------------------- definitions
def test_the_three_playbooks_the_prd_names_all_exist() -> None:
    for required in ("FireResponsePlaybook", "IntrusionPlaybook", "DwellEscalationPlaybook"):
        assert required in PLAYBOOKS


def test_the_fire_playbook_has_the_five_steps_the_prd_lists() -> None:
    steps = [step.step_id for step in FIRE_RESPONSE.steps]
    assert steps == [
        "dispatch_drone",
        "notify_security",
        "close_gate",
        "create_incident",
        "generate_report",
    ]


def test_every_step_either_compensates_or_says_it_cannot() -> None:
    """A step that changes something and declares neither is a silent gap in every rollback."""
    for playbook in PLAYBOOKS.values():
        for step in playbook.steps:
            has_undo = step.compensate is not None
            declared = step.irreversible or step.optional
            assert has_undo or declared, (
                f"{playbook.name}.{step.step_id} neither compensates nor declares why not"
            )


def test_a_step_cannot_be_both_compensable_and_irreversible() -> None:
    with pytest.raises(ValueError, match="both compensable and irreversible"):
        StepSpec(
            step_id="x", name="x", activity="close_gate", compensate="open_gate", irreversible=True
        )


def test_every_referenced_activity_exists() -> None:
    """A playbook naming a missing activity fails at run time, in the middle of an incident."""
    for playbook in PLAYBOOKS.values():
        for step in playbook.steps:
            assert step.activity in ACTIVITIES, f"{playbook.name}.{step.step_id}: {step.activity}"
            if step.compensate:
                assert step.compensate in ACTIVITIES, f"{playbook.name}.{step.step_id} compensation"


def test_severity_is_compared_by_rank_not_alphabetically() -> None:
    """ "high" is not greater than "critical" alphabetically, and a playbook that failed to fire on the most
    severe events would be a very quiet bug."""
    assert playbooks_for("fire_detected", "critical"), (
        "critical must trigger a high-threshold playbook"
    )
    assert playbooks_for("fire_detected", "high")
    assert not playbooks_for("fire_detected", "medium")
    assert not playbooks_for("fire_detected", "info")


# ------------------------------------------------------------------ forward path
async def test_a_fire_event_drives_all_five_steps_with_progress() -> None:
    """The PRD's acceptance criterion: five steps, visible one at a time.

    A five-step response that reports only "running" and then "completed" is indistinguishable from a hang
    while it is happening, which is precisely when someone is watching.
    """
    seen: list[tuple[str, str]] = []

    async def on_progress(run: Any, step: Any) -> None:
        seen.append((step.step_id, str(step.status)))

    outcome = await InlineRunner(recording_activities()).execute(
        FIRE_RESPONSE, a_context(), trigger_event_id="evt_fire", on_progress=on_progress
    )

    assert outcome.ok
    assert outcome.run.status == RunStatus.COMPLETED
    assert [step.step_id for step in outcome.run.steps] == [
        "dispatch_drone",
        "notify_security",
        "close_gate",
        "create_incident",
        "generate_report",
    ]
    assert all(step.status == RunStatus.COMPLETED for step in outcome.run.steps)
    assert outcome.run.progress == 1.0
    assert outcome.run.trigger_event == "evt_fire"

    # Each step reported RUNNING then COMPLETED, in order — ten updates for five steps.
    assert len(seen) == 10
    assert seen[0] == ("dispatch_drone", "running")
    assert seen[1] == ("dispatch_drone", "completed")
    assert seen[-1] == ("generate_report", "completed")


async def test_steps_run_in_order() -> None:
    order: list[str] = []
    await InlineRunner(recording_activities(order=order)).execute(FIRE_RESPONSE, a_context())
    assert order == [
        "dispatch_drone",
        "notify_security",
        "close_gate",
        "create_incident",
        "generate_report",
    ]


# ------------------------------------------------------------------ compensation
async def test_a_failure_undoes_completed_steps_in_reverse_order() -> None:
    """Reverse because the dependencies run that way: a gate closed after a drone was dispatched must be
    reopened before the drone is recalled, or there is a window where the site is sealed and nothing is
    watching it.
    """
    order: list[str] = []
    outcome = await InlineRunner(
        recording_activities(fail={"create_incident"}, order=order)
    ).execute(fast(FIRE_RESPONSE), a_context())

    assert not outcome.ok
    assert outcome.run.status == RunStatus.FAILED

    # The forward path, ignoring repeats: create_incident legitimately appears three times, because it was
    # retried to its budget before the run gave up. Asserting on a slice assumed one attempt per step,
    # which is exactly the behaviour under test elsewhere.
    forward: list[str] = []
    for name in order:
        if name in ("open_gate", "recall_drone"):
            break
        if not forward or forward[-1] != name:
            forward.append(name)
    assert forward == ["dispatch_drone", "notify_security", "close_gate", "create_incident"]

    # Compensations, in reverse of the order the steps ran. notify_security is irreversible, so it is
    # acknowledged rather than undone, and close_gate must be reopened BEFORE the drone is recalled.
    compensations = [
        name for name in order if name in ("open_gate", "recall_drone", "close_incident")
    ]
    assert compensations == ["open_gate", "recall_drone"], compensations
    assert "close_incident" not in order, "the failed step is not compensated"
    assert "close_gate" in outcome.compensated
    assert "dispatch_drone" in outcome.compensated


async def test_an_irreversible_step_is_acknowledged_not_passed_over() -> None:
    """ "Nothing to undo" and "cannot be undone" are different facts, and only one is safe to pass over in
    silence."""
    outcome = await InlineRunner(recording_activities(fail={"close_gate"})).execute(
        fast(FIRE_RESPONSE), a_context()
    )
    assert any(
        "notify_security" in entry and "irreversible" in entry for entry in outcome.compensated
    ), outcome.compensated


async def test_the_failed_step_itself_is_not_compensated() -> None:
    """Undoing something that did not happen is at best a no-op and at worst a second action."""
    order: list[str] = []
    await InlineRunner(recording_activities(fail={"close_gate"}, order=order)).execute(
        fast(FIRE_RESPONSE), a_context()
    )
    assert "open_gate" not in order, "close_gate failed, so there is nothing to reopen"


async def test_a_failed_compensation_escalates() -> None:
    """The worst case: a gate closed by a response that then rolled back, and no automated path left to
    open it. The world is in a state nobody chose."""
    activities = recording_activities(fail={"create_incident", "open_gate"})
    outcome = await InlineRunner(activities).execute(fast(FIRE_RESPONSE), a_context())

    assert outcome.needs_human, "a failed compensation must demand a human"
    assert any("close_gate" in failure for failure in outcome.compensation_failures)
    # And the compensations after it still run: one broken undo must not abandon the rest.
    assert "dispatch_drone" in outcome.compensated


async def test_a_missing_compensation_activity_is_reported_not_ignored() -> None:
    activities = recording_activities(fail={"create_incident"})
    del activities["open_gate"]
    outcome = await InlineRunner(activities).execute(fast(FIRE_RESPONSE), a_context())
    assert outcome.needs_human
    assert any("open_gate" in failure for failure in outcome.compensation_failures)


# ----------------------------------------------------------- optional and retries
async def test_an_optional_step_failing_does_not_roll_back_the_response() -> None:
    """A report that will not render must not undo a drone dispatch and a gate closure."""
    order: list[str] = []
    outcome = await InlineRunner(
        recording_activities(fail={"generate_report"}, order=order)
    ).execute(fast(FIRE_RESPONSE), a_context())

    assert outcome.ok, "the run should still complete"
    assert outcome.compensated == [], "nothing should have been undone"
    report = next(step for step in outcome.run.steps if step.step_id == "generate_report")
    assert report.status == RunStatus.FAILED, "and the failure is recorded honestly, not softened"
    assert report.error


async def test_a_step_is_retried_up_to_its_budget() -> None:
    attempts = {"count": 0}

    async def flaky(context: ActivityContext, step_id: str, **arguments: Any) -> dict[str, Any]:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("not yet")
        return {"ok": True}

    playbook = Playbook(
        name="Flaky",
        description="t",
        steps=(
            StepSpec(
                step_id="s", name="s", activity="dispatch_drone", max_attempts=3, backoff_s=0.01
            ),
        ),
    )
    outcome = await InlineRunner({"dispatch_drone": flaky}).execute(playbook, a_context())
    assert outcome.ok
    assert outcome.run.steps[0].attempts == 3


async def test_a_step_that_never_succeeds_fails_after_its_budget() -> None:
    calls = {"count": 0}

    async def broken(context: ActivityContext, step_id: str, **arguments: Any) -> dict[str, Any]:
        calls["count"] += 1
        raise RuntimeError("always")

    playbook = Playbook(
        name="Broken",
        description="t",
        steps=(
            StepSpec(
                step_id="s", name="s", activity="dispatch_drone", max_attempts=2, backoff_s=0.01
            ),
        ),
    )
    outcome = await InlineRunner({"dispatch_drone": broken}).execute(playbook, a_context())
    assert not outcome.ok
    assert calls["count"] == 2, "exactly the budget, no more"
    assert "always" in (outcome.run.steps[0].error or "")


async def test_a_hanging_step_times_out_rather_than_stalling_the_response() -> None:
    playbook = Playbook(
        name="Hang",
        description="t",
        steps=(
            StepSpec(
                step_id="s",
                name="s",
                activity="dispatch_drone",
                timeout_s=0.2,
                max_attempts=1,
                backoff_s=0.01,
            ),
        ),
    )
    outcome = await InlineRunner(recording_activities(hang={"dispatch_drone"})).execute(
        playbook, a_context()
    )
    assert not outcome.ok
    assert "timed out" in (outcome.run.steps[0].error or "")


async def test_a_missing_activity_fails_immediately_without_retrying() -> None:
    """A definition error is not a transient one; retrying it three times reaches the same conclusion
    slower."""
    playbook = Playbook(
        name="Missing",
        description="t",
        steps=(StepSpec(step_id="s", name="s", activity="does_not_exist", max_attempts=3),),
    )
    outcome = await InlineRunner({}).execute(playbook, a_context())
    assert not outcome.ok
    assert outcome.run.steps[0].attempts == 0
    assert "no activity named" in (outcome.run.steps[0].error or "")


# ------------------------------------------------------------------ idempotency
async def test_a_retried_activity_does_not_act_twice() -> None:
    """A timeout means the step MAY ALREADY HAVE ACTED. Retrying without idempotency double-dispatches the
    drone, and the retry is the expected path, not the exceptional one."""
    calls = {"count": 0}

    @idempotent
    async def counted(context: ActivityContext, step_id: str, **arguments: Any) -> dict[str, Any]:
        calls["count"] += 1
        return {"dispatched": True}

    context = a_context()
    first = await counted(context, "dispatch_drone")
    second = await counted(context, "dispatch_drone")

    assert calls["count"] == 1, "the effect happened once"
    assert first["dispatched"] is True
    assert second["idempotent_replay"] is True, "and the replay says so"


async def test_idempotency_is_per_run_not_global() -> None:
    """Two fires should dispatch two drones."""
    calls = {"count": 0}

    @idempotent
    async def counted(context: ActivityContext, step_id: str, **arguments: Any) -> dict[str, Any]:
        calls["count"] += 1
        return {"ok": True}

    await counted(a_context(run_id="wfr_one"), "dispatch_drone")
    await counted(a_context(run_id="wfr_two"), "dispatch_drone")
    assert calls["count"] == 2


# --------------------------------------------------------------------- cooldowns
def test_a_burning_fire_does_not_start_a_hundred_playbooks() -> None:
    """`fire_detected` fires on nearly every frame while a fire burns. The same lesson as the events
    engine's cooldowns, one layer up: without a gap, one fire dispatches one drone a hundred times."""
    ledger = RunLedger()
    assert ledger.may_start(FIRE_RESPONSE, "dock_3") is True
    for _ in range(50):
        assert ledger.may_start(FIRE_RESPONSE, "dock_3") is False
    assert ledger.suppressed == 50
    # A different zone is a different fire.
    assert ledger.may_start(FIRE_RESPONSE, "dock_5") is True


def test_the_ledger_keeps_recent_runs_bounded() -> None:
    from sio_schemas import WorkflowRun

    ledger = RunLedger(keep=5)
    for index in range(20):
        ledger.record(WorkflowRun(tenant_id="acme", playbook="P", run_id=f"wfr_{index}"))
    assert len(ledger.runs) == 5
    assert ledger.get("wfr_19") is not None
    assert ledger.get("wfr_0") is None


# -------------------------------------------------------------- progress safety
async def test_a_broken_progress_hook_does_not_fail_the_response() -> None:
    """Progress is observability. A UI subscriber that raises must not roll back a fire response."""

    async def exploding(run: Any, step: Any) -> None:
        raise RuntimeError("the subscriber is broken")

    outcome = await InlineRunner(recording_activities()).execute(
        FIRE_RESPONSE, a_context(), on_progress=exploding
    )
    assert outcome.ok


# ------------------------------------------------------------------- activities
async def test_activities_report_honestly_when_there_is_no_actuator() -> None:
    """There is no drone command interface in this build. Reporting a dispatch that did not happen would
    make the run record a lie, and the run record is what an incident review reads."""
    context = a_context(dry_run=False)
    result = await ACTIVITIES["dispatch_drone"](context, "dispatch_drone")
    assert result["dispatched"] is False
    assert "note" in result and "no drone command interface" in result["note"]


async def test_dry_run_describes_without_acting() -> None:
    context = a_context(dry_run=True)
    result = await ACTIVITIES["close_gate"](context, "close_gate")
    assert result["dry_run"] is True
    assert "would" in result


async def test_compensating_an_incident_resolves_it_rather_than_deleting_it() -> None:
    """Deleting it would erase the fact that a response started, which is exactly what the append-only
    tables exist to prevent."""
    result = await ACTIVITIES["close_incident"](a_context(), "create_incident:compensate")
    assert result["resolved"] is True
    assert "rolled back" in result["reason"]


def test_gate_selection_does_not_reimplement_geometry() -> None:
    """The spatial service owns geometry; a workflow forming a second opinion on which gate is nearest
    would be a second implementation of an answered question."""
    from sio_workflow.activities import _nearest_gate

    assert _nearest_gate("dock_3") == "gate_b"
    assert _nearest_gate("yard") == "gate_a"
    assert _nearest_gate(None) == "gate_a", (
        "an unknown zone gets the main gate, and the record shows it"
    )
