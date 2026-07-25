"""Running a playbook: retries, timeouts, compensation, and progress worth watching (PRD M15).

`InlineRunner` executes the activity graph in-process. It is not a stub for Temporal — it is the runner CI
and the demo use, and it implements the semantics that actually matter:

* **per-step timeout**, because a step that hangs stalls a response nobody is watching;
* **bounded retries with backoff**, because a timeout is how a slow network looks;
* **compensation in reverse order** when a step fails, because "failed" is not a state anyone can act on
  if a gate is still shut;
* **progress emitted per step**, because a five-step response that reports only "running" and then
  "failed" is indistinguishable from a hang.

What Temporal adds — durability across a process death — is real and is why the `Runner` port exists. What
it does not add is any of the above, and hiding these semantics inside a framework would make them
untestable without one.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from sio_core import get_logger
from sio_schemas import RunStatus, WorkflowRun, WorkflowStep, new_id, utc_now

from .activities import ACTIVITIES, ActivityContext
from .playbooks import Playbook, StepSpec

log = get_logger("sio.workflow.runner")

ProgressHook = Callable[[WorkflowRun, WorkflowStep], Awaitable[None]]


@dataclass
class RunOutcome:
    """The result of a run, including what had to be undone."""

    run: WorkflowRun
    compensated: list[str] = field(default_factory=list)
    compensation_failures: list[str] = field(default_factory=list)
    """Compensations that themselves failed. The worst case, and the one that must be loudest.

    A failed compensation means the world is in a state nobody chose: a gate closed by a response that then
    rolled back, and no automated path left to open it. It escalates rather than being logged.
    """

    @property
    def ok(self) -> bool:
        return self.run.status == RunStatus.COMPLETED

    @property
    def needs_human(self) -> bool:
        return bool(self.compensation_failures)


class Runner(Protocol):
    """The seam. Anything that can execute a playbook and report progress."""

    name: str

    async def execute(
        self,
        playbook: Playbook,
        context: ActivityContext,
        *,
        trigger_event_id: str | None = None,
        on_progress: ProgressHook | None = None,
    ) -> RunOutcome: ...


class InlineRunner:
    """Executes a playbook in this process."""

    name = "inline"

    def __init__(self, activities: dict[str, Any] | None = None) -> None:
        self.activities = activities or ACTIVITIES

    async def execute(
        self,
        playbook: Playbook,
        context: ActivityContext,
        *,
        trigger_event_id: str | None = None,
        on_progress: ProgressHook | None = None,
    ) -> RunOutcome:
        run = WorkflowRun(
            run_id=context.run_id or new_id("wfr"),
            tenant_id=context.tenant_id,
            playbook=playbook.name,
            status=RunStatus.RUNNING,
            trigger_event=trigger_event_id,
            runner=self.name,
            entity_ids=list(context.entity_ids),
            steps=[
                WorkflowStep(step_id=step.step_id, name=step.name, status=RunStatus.PENDING)
                for step in playbook.steps
            ],
        )
        outcome = RunOutcome(run=run)
        completed: list[StepSpec] = []

        for index, spec in enumerate(playbook.steps):
            step = run.steps[index]
            step.status = RunStatus.RUNNING
            step.started_ts = utc_now()
            await _notify(on_progress, run, step)

            ok, output, error, attempts = await self._run_step(spec, context)
            step.attempts = attempts
            step.finished_ts = utc_now()
            step.output = output
            step.error = error

            if ok:
                step.status = RunStatus.COMPLETED
                completed.append(spec)
                await _notify(on_progress, run, step)
                continue

            if spec.optional:
                # The step FAILED and the run continues. Recorded as failed rather than as some softer
                # status, because it did fail — inventing a "skipped" state would hide a real failure
                # behind a word that sounds deliberate. What is different is the RUN's outcome, not the
                # step's: a report that would not render must not roll back a response that has already
                # dispatched a drone.
                step.status = RunStatus.FAILED
                log.warning(
                    "workflow.optional_step_failed",
                    step=spec.step_id,
                    error=error,
                    effect="the run continues; this step is optional",
                )
                await _notify(on_progress, run, step)
                continue

            step.status = RunStatus.FAILED
            await _notify(on_progress, run, step)
            log.error("workflow.step_failed", step=spec.step_id, error=error, attempts=attempts)
            await self._compensate(completed, context, outcome, run, on_progress)
            run.status = RunStatus.FAILED
            run.finished_ts = utc_now()
            return outcome

        run.status = RunStatus.COMPLETED
        run.finished_ts = utc_now()
        return outcome

    async def _run_step(
        self, spec: StepSpec, context: ActivityContext
    ) -> tuple[bool, dict[str, Any], str | None, int]:
        """Run one step with its timeout and retry budget."""
        activity = self.activities.get(spec.activity)
        if activity is None:
            # A missing activity is a definition error, not a runtime one, so it fails immediately rather
            # than being retried three times on the way to the same conclusion.
            return False, {}, f"no activity named {spec.activity!r}", 0

        last_error: str | None = None
        for attempt in range(1, spec.max_attempts + 1):
            try:
                output = await asyncio.wait_for(
                    activity(context, spec.step_id, **spec.arguments),
                    timeout=spec.timeout_s,
                )
                return (
                    True,
                    output if isinstance(output, dict) else {"result": output},
                    None,
                    attempt,
                )
            except TimeoutError:
                # Named separately from other failures: a timeout means the step MAY HAVE ACTED, which is
                # why activities are idempotent on a run-and-step key. Retrying without that would
                # double-dispatch.
                last_error = f"timed out after {spec.timeout_s:.0f}s"
                log.warning(
                    "workflow.step_timeout",
                    step=spec.step_id,
                    attempt=attempt,
                    timeout_s=spec.timeout_s,
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "workflow.step_error", step=spec.step_id, attempt=attempt, error=last_error
                )

            if attempt < spec.max_attempts:
                # Exponential backoff. A step failing because a service is restarting wants a second
                # attempt later, not immediately — three immediate retries are one retry with extra logs.
                await asyncio.sleep(spec.backoff_s * (2 ** (attempt - 1)))
        return False, {}, last_error, spec.max_attempts

    async def _compensate(
        self,
        completed: list[StepSpec],
        context: ActivityContext,
        outcome: RunOutcome,
        run: WorkflowRun,
        on_progress: ProgressHook | None,
    ) -> None:
        """Undo completed steps in reverse order.

        Reverse because the dependencies run that way: a gate closed after a drone was dispatched must be
        reopened before the drone is recalled, or there is a window where the site is sealed and nothing is
        watching it.

        Compensation deliberately does NOT retry to the same depth as the forward path. A compensation that
        fails is escalated immediately, because the useful action is a human, and three more attempts is
        three more minutes of a gate being shut.
        """
        for spec in reversed(completed):
            if spec.irreversible:
                outcome.compensated.append(f"{spec.step_id} (irreversible; acknowledged)")
                log.info("workflow.step_irreversible", step=spec.step_id)
                continue
            if not spec.compensate:
                continue
            activity = self.activities.get(spec.compensate)
            if activity is None:
                outcome.compensation_failures.append(
                    f"{spec.step_id}: no activity named {spec.compensate!r}"
                )
                continue
            try:
                await asyncio.wait_for(
                    activity(context, f"{spec.step_id}:compensate", **spec.arguments),
                    timeout=spec.timeout_s,
                )
                outcome.compensated.append(spec.step_id)
                step = next((s for s in run.steps if s.step_id == spec.step_id), None)
                if step is not None:
                    step.status = RunStatus.COMPENSATED
                    await _notify(on_progress, run, step)
                log.info("workflow.step_compensated", step=spec.step_id)
            except Exception as exc:
                outcome.compensation_failures.append(f"{spec.step_id}: {type(exc).__name__}: {exc}")
                log.error(
                    "workflow.compensation_failed",
                    step=spec.step_id,
                    error=str(exc),
                    effect="the world is in a state nobody chose; this needs a human",
                )


async def _notify(hook: ProgressHook | None, run: WorkflowRun, step: WorkflowStep) -> None:
    """Emit progress, and never let a broken hook fail the run.

    Progress is observability. A UI subscriber that raises must not roll back a fire response, which is
    exactly what an unguarded await here would cause.
    """
    if hook is None:
        return
    try:
        await hook(run, step)
    except Exception as exc:
        log.warning("workflow.progress_hook_failed", error=str(exc))


class RunLedger:
    """Recent runs and per-subject cooldowns.

    The cooldown is the same lesson as the events engine, one layer up: a fire produces `fire_detected` on
    nearly every frame while it burns, and without a gap a single fire starts a hundred playbooks, each
    dispatching the same drone.
    """

    def __init__(self, *, keep: int = 100) -> None:
        self.keep = keep
        self.runs: list[WorkflowRun] = []
        self._last_started: dict[tuple[str, str], float] = {}
        self.suppressed = 0

    def may_start(self, playbook: Playbook, subject: str) -> bool:
        key = (playbook.name, subject)
        last = self._last_started.get(key)
        now = time.monotonic()
        if last is not None and now - last < playbook.cooldown_s:
            self.suppressed += 1
            return False
        self._last_started[key] = now
        return True

    def record(self, run: WorkflowRun) -> None:
        self.runs.append(run)
        del self.runs[: max(0, len(self.runs) - self.keep)]

    def get(self, run_id: str) -> WorkflowRun | None:
        return next((run for run in self.runs if run.run_id == run_id), None)

    def describe(self) -> dict[str, Any]:
        return {
            "runs": len(self.runs),
            "suppressed_by_cooldown": self.suppressed,
            "recent": [
                {
                    "run_id": run.run_id,
                    "playbook": run.playbook,
                    "status": str(run.status),
                    "progress": round(run.progress, 2),
                    "started": run.started_ts.isoformat(),
                    "steps": [
                        {"name": step.name, "status": str(step.status), "attempts": step.attempts}
                        for step in run.steps
                    ],
                }
                for run in reversed(self.runs[-10:])
            ],
        }


__all__ = ["InlineRunner", "ProgressHook", "RunLedger", "RunOutcome", "Runner"]
