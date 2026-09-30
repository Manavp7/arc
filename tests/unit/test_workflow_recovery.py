"""Crash-boundary behavior: checkpoints are authoritative, effects are not assumed."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict

import pytest
from sio_workflow.activities import ActivityContext
from sio_workflow.durable import DurableRunner
from sio_workflow.playbooks import Playbook, StepSpec

from sio_schemas import RunStatus, WorkflowRun, WorkflowStep


class MemoryExecution:
    def __init__(self, playbook, checkpoint=None, state="queued"):
        run = WorkflowRun(
            tenant_id="test",
            run_id="wfr_test",
            playbook=playbook.name,
            steps=[WorkflowStep(step_id=s.step_id, name=s.name) for s in playbook.steps],
        )
        self.checkpoint = (
            deepcopy(checkpoint)
            if checkpoint
            else {
                "run": run.to_wire(),
                "inflight": None,
                "compensated": [],
                "compensation_failures": [],
                "needs_human": False,
            }
        )
        self.run = WorkflowRun.model_validate(self.checkpoint["run"])
        self.record = {"definition": asdict(playbook), "state": state}
        self.saved = deepcopy(self.checkpoint)
        self.saved_state = state
        self.fail = False

    async def save(self, state):
        if self.fail:
            raise RuntimeError("authored disk failure")
        self.checkpoint["run"] = self.run.to_wire()
        self.saved = deepcopy(self.checkpoint)
        self.record["state"] = self.saved_state = state


def context():
    return ActivityContext(
        api_url="http://unused", ingest_url="http://unused", tenant_id="test", run_id="wfr_test"
    )


def safe(function):
    function.recovery_safe = True
    return function


async def test_restart_skips_completed_steps_and_resumes_safe_interrupted_step():
    calls = []

    @safe
    async def first(ctx, key):
        calls.append(key)
        return {"saved": True}

    @safe
    async def interrupted(ctx, key):
        calls.append(key)
        raise asyncio.CancelledError

    playbook = Playbook(
        "test",
        "authored",
        (
            StepSpec("one", "One", "first"),
            StepSpec("two", "Two", "second", max_attempts=3, backoff_s=0),
        ),
    )
    execution = MemoryExecution(playbook)
    with pytest.raises(asyncio.CancelledError):
        await DurableRunner({"first": first, "second": interrupted}).execute(execution, context())
    assert execution.saved["run"]["steps"][0]["status"] == "completed"
    assert execution.saved["inflight"]["step_id"] == "two"

    @safe
    async def recovered(ctx, key):
        calls.append(key)
        return {"saved": True}

    restarted = MemoryExecution(playbook, execution.saved, execution.saved_state)
    result = await DurableRunner({"first": first, "second": recovered}).execute(
        restarted, context()
    )
    assert result.ok and calls == ["one", "two", "two"]
    assert result.run.steps[1].attempts == 2


async def test_uncertain_effect_is_held_without_retry_or_compensation():
    calls = []

    async def external(ctx, key):
        calls.append(key)
        raise asyncio.CancelledError

    playbook = Playbook("test", "authored", (StepSpec("send", "Send", "external"),))
    execution = MemoryExecution(playbook)
    with pytest.raises(asyncio.CancelledError):
        await DurableRunner({"external": external}).execute(execution, context())
    restarted = MemoryExecution(playbook, execution.saved, execution.saved_state)
    outcome = await DurableRunner({"external": external}).execute(restarted, context())
    assert calls == ["send"]
    assert outcome.needs_human and not outcome.ok
    assert restarted.saved_state == "needs_human"


async def test_timeout_of_unsafe_effect_does_not_use_retry_budget():
    calls = []

    async def external(ctx, key):
        calls.append(key)
        raise TimeoutError

    playbook = Playbook("test", "authored", (StepSpec("send", "Send", "external", max_attempts=5),))
    execution = MemoryExecution(playbook)
    result = await DurableRunner({"external": external}).execute(execution, context())
    assert calls == ["send"] and result.needs_human


@pytest.mark.parametrize("compensating", [False, True])
async def test_error_result_from_unsafe_activity_stops_for_reconciliation(compensating):
    calls = []

    @safe
    async def saved(ctx, key):
        calls.append(key)
        return {"saved": True}

    @safe
    async def failure(ctx, key):
        calls.append(key)
        return {"error": "confirmed local failure"}

    async def unconfirmed(ctx, key):
        calls.append(key)
        return {"error": "receiver status unknown"}

    playbook = Playbook(
        "test",
        "authored",
        (
            StepSpec("one", "One", "saved", compensate="unconfirmed" if compensating else "saved"),
            StepSpec("two", "Two", "failure" if compensating else "unconfirmed", max_attempts=5),
        ),
    )
    execution = MemoryExecution(playbook)
    result = await DurableRunner(
        {"saved": saved, "failure": failure, "unconfirmed": unconfirmed}
    ).execute(execution, context())
    assert result.needs_human and execution.saved_state == "needs_human"
    assert calls == (["one", "two", "one:compensate"] if compensating else ["one", "two"])
    assert execution.saved["inflight"]["activity"] == "unconfirmed"
    assert result.compensated == []


async def test_checkpoint_failure_prevents_effect():
    calls = []

    async def external(ctx, key):
        calls.append(key)
        return {}

    playbook = Playbook("test", "authored", (StepSpec("send", "Send", "external"),))
    execution = MemoryExecution(playbook)
    execution.fail = True
    with pytest.raises(RuntimeError, match="disk failure"):
        await DurableRunner({"external": external}).execute(execution, context())
    assert calls == []


async def test_restart_during_compensation_never_repeats_completed_undo():
    calls = []

    @safe
    async def work(ctx, key):
        return {}

    @safe
    async def failure(ctx, key):
        return {"error": "authored failure"}

    @safe
    async def undo(ctx, key):
        calls.append(key)
        if key == "one:compensate":
            raise asyncio.CancelledError
        return {"undone": True}

    playbook = Playbook(
        "test",
        "authored",
        (
            StepSpec("one", "One", "work", compensate="undo"),
            StepSpec("two", "Two", "work", compensate="undo"),
            StepSpec("fail", "Fail", "failure"),
        ),
    )
    execution = MemoryExecution(playbook)
    with pytest.raises(asyncio.CancelledError):
        await DurableRunner({"work": work, "failure": failure, "undo": undo}).execute(
            execution, context()
        )
    assert execution.saved["run"]["steps"][1]["status"] == "compensated"
    restarted = MemoryExecution(playbook, execution.saved, execution.saved_state)
    outcome = await DurableRunner({"work": work, "failure": failure, "undo": undo}).execute(
        restarted, context()
    )
    assert calls.count("two:compensate") == 1
    assert outcome.run.status == RunStatus.FAILED
    assert outcome.needs_human  # interrupted compensation consumed its one attempt
