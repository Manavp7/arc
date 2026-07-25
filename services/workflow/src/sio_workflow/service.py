"""Workflow service: events in, response playbooks out, progress visible (PRD M15)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from sio_core import MessageContext, PgPool, SioService, get_pg_pool
from sio_core.explain import ExplanationBuilder
from sio_schemas import (
    BusMessage,
    Event,
    EventType,
    RunStatus,
    Severity,
    Topic,
    WorkflowRun,
    WorkflowStep,
    new_id,
    utc_now,
)

from .activities import ActivityContext
from .nocode import (
    OPERATORS,
    SEVERITIES,
    Problem,
    WorkflowSpec,
    load_workflows,
    parse,
    to_playbook,
    validate,
)
from .playbooks import PLAYBOOKS, Playbook, playbooks_for
from .runner import InlineRunner, RunLedger, RunOutcome


def _severity_rank(severity: str) -> int:
    """Where a severity sits in the ordering, unknown values sorting lowest.

    Lowest rather than highest: an unrecognised severity should not clear a `min_severity: critical` gate. A
    typo must fail closed.
    """
    return SEVERITIES.index(severity) if severity in SEVERITIES else -1


class WorkflowService(SioService):
    """Turns high-severity events into response playbooks, and shows each step as it happens."""

    name = "workflow"
    subscribes = (Topic.EVENTS,)
    tick_interval_s = 60.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.pool: PgPool = get_pg_pool(self.settings)
        self.runner = InlineRunner()
        self.ledger = RunLedger()
        self._authored: dict[str, WorkflowSpec] = {}
        self._authored_problems: list[Problem] = []
        self._started = 0
        self._completed = 0
        self._failed = 0
        self._needing_human = 0

    async def setup(self) -> None:
        await self.pool.open()
        self._reload_authored()
        self.log.info(
            "workflow.ready",
            runner=self.runner.name,
            playbooks=sorted(PLAYBOOKS),
            authored=sorted(self._authored),
            authored_problems=len(self._authored_problems),
            dry_run=self.settings.workflow_dry_run,
        )
        for problem in self._authored_problems:
            # Logged individually and loudly. A workflow that failed to load is indistinguishable from one that
            # never fires, and the author will assume the latter.
            self.log.warning(
                "workflow.authored_rejected",
                where=problem.where,
                why=problem.message,
                fix=problem.fix,
            )
        if self.settings.workflow_dry_run:
            self.log.info(
                "workflow.dry_run",
                effect="steps describe what they would do without doing it",
                why="the default, because a workflow engine that can only be tested by closing a real "
                "gate is one nobody tests",
            )

    async def health_checks(self) -> dict[str, str]:
        checks = {"postgres": "ok" if await self.pool.ping() else "unreachable"}
        if self._needing_human:
            # A failed compensation leaves the world in a state nobody chose. That must not be a log line
            # nobody reads — it degrades health until someone looks.
            checks["compensation"] = (
                f"degraded: {self._needing_human} run(s) had a compensation fail and need a human"
            )
        return checks

    async def health_info(self) -> dict[str, str]:
        return {
            "runs_started": str(self._started),
            "completed": str(self._completed),
            "failed": str(self._failed),
            "suppressed_by_cooldown": str(self.ledger.suppressed),
            "needing_human": str(self._needing_human),
            "runner": self.runner.name,
        }

    # ------------------------------------------------------------------ handling
    @property
    def workflow_dir(self) -> Path:
        """Where authored workflows live.

        Under `.sio/` rather than in the package, because they are site configuration written by an operator, not
        code — and a file an operator edits should never be inside something `pip install` overwrites.
        """
        return Path(self.settings.data_dir) / "workflows"

    def _reload_authored(self) -> None:
        specs, problems = load_workflows(self.workflow_dir)
        # Collisions with a code playbook are rejected rather than merged, in that direction: an authored file
        # silently overriding the fire response would be a way to disable it without anybody noticing.
        self._authored = {spec.name: spec for spec in specs if spec.name not in PLAYBOOKS}
        self._authored_problems = list(problems)
        for spec in specs:
            if spec.name in PLAYBOOKS:
                self._authored_problems.append(
                    Problem(
                        spec.name,
                        f"{spec.name!r} is also a code playbook, so this file was ignored",
                        "rename it; an authored file must not be able to silently replace a code playbook",
                    )
                )

    def _playbooks_for(self, event: Event, message: BusMessage) -> list[Playbook]:
        """Code playbooks, plus authored workflows whose conditions pass.

        One execution path for both. A separate one for no-code workflows would mean retries, compensation,
        cooldowns and run records had a second implementation that drifts — and the no-code one would be the
        less-tested of the pair while dispatching the same drones.
        """
        selected = list(playbooks_for(str(event.type), str(event.severity)))
        if not self._authored:
            return selected

        fact = {
            "type": str(event.type),
            "severity": str(event.severity),
            "zone_id": event.zone_id,
            "payload": message.payload if isinstance(message.payload, dict) else {},
            **(event.model_dump(mode="json") if hasattr(event, "model_dump") else {}),
        }
        for spec in self._authored.values():
            if not spec.enabled or str(event.type) not in spec.event_types:
                continue
            if _severity_rank(str(event.severity)) < _severity_rank(spec.min_severity):
                continue
            if not spec.matches(fact):
                self.log.debug(
                    "workflow.conditions_failed", workflow=spec.name, event_type=str(event.type)
                )
                continue
            try:
                selected.append(to_playbook(spec))
            except ValueError as error:
                # An authored workflow that has become invalid — an activity removed by a later deploy, say —
                # must not stop the code playbooks from running.
                self.log.warning("workflow.authored_invalid", workflow=spec.name, why=str(error))
        return selected

    async def on_message(self, message: BusMessage, ctx: MessageContext) -> None:
        if message.kind != "Event":
            return
        event = message.decode(Event)
        if event.rule_id and event.rule_id.startswith("workflow."):
            # A playbook's own progress events come back around on this topic. Reacting to them would have
            # a fire response trigger a fire response.
            return

        for playbook in self._playbooks_for(event, message):
            subject = self._subject(playbook, event)
            if not self.ledger.may_start(playbook, subject):
                self.log.info(
                    "workflow.suppressed",
                    playbook=playbook.name,
                    subject=subject,
                    why=f"within the {playbook.cooldown_s:.0f}s cooldown",
                )
                continue
            await self._run(playbook, event, ctx)

    @staticmethod
    def _subject(playbook: Playbook, event: Event) -> str:
        parts = []
        for key in playbook.key_by:
            if key == "zone_id":
                parts.append(event.zone_id or "")
            elif key == "entity_id":
                parts.append(event.entities[0] if event.entities else "")
        return "|".join(parts) or "*"

    async def _run(
        self, playbook: Playbook, event: Event, ctx: MessageContext | None
    ) -> RunOutcome:
        run_id = new_id("wfr")
        context = ActivityContext(
            api_url=f"http://127.0.0.1:{self.settings.api_port}",
            ingest_url=f"http://127.0.0.1:{self.settings.ingest_port}",
            tenant_id=event.tenant_id,
            run_id=run_id,
            trigger_event_id=event.event_id,
            zone_id=event.zone_id,
            entity_ids=list(event.entities),
            dry_run=self.settings.workflow_dry_run,
        )
        self._started += 1
        self.log.info(
            "workflow.started",
            run_id=run_id,
            playbook=playbook.name,
            trigger=str(event.type),
            zone=event.zone_id,
            steps=playbook.step_count,
        )

        async def on_progress(run: WorkflowRun, step: WorkflowStep) -> None:
            await self._publish_step(run, step, playbook, ctx)
            await self._persist(run)

        outcome = await self.runner.execute(
            playbook, context, trigger_event_id=event.event_id, on_progress=on_progress
        )
        await context.close()

        outcome.run.explanation = self._explain(playbook, outcome, event)
        await self._persist(outcome.run)
        self.ledger.record(outcome.run)
        if outcome.ok:
            self._completed += 1
        else:
            self._failed += 1
        if outcome.needs_human:
            self._needing_human += 1
            self.log.error(
                "workflow.needs_human",
                run_id=run_id,
                failures=outcome.compensation_failures,
                effect="the world is in a state nobody chose",
            )
        await self._publish_summary(outcome, playbook, ctx)
        return outcome

    # ---------------------------------------------------------------- publishing
    async def _publish_step(
        self, run: WorkflowRun, step: WorkflowStep, playbook: Playbook, ctx: MessageContext | None
    ) -> None:
        """One event per step transition, so the console can show a response happening.

        A five-step response that reports only "running" and then "completed" is indistinguishable from a
        hang while it is happening, which is exactly when someone is watching it.
        """
        explanation = ExplanationBuilder(summary=f"{playbook.name}: {step.name} — {step.status}")
        explanation.add_rule(f"workflow.{playbook.name}", note=playbook.description)
        explanation.add_note(
            f"step {step.step_id} of {playbook.step_count}, attempt {step.attempts}"
        )
        if step.error:
            explanation.add_note(f"error: {step.error}")
        if step.output.get("dry_run"):
            explanation.add_note("dry run: the step described what it would do without doing it")
        if step.output.get("would"):
            explanation.add_note(f"would: {step.output['would']}")

        event = Event(
            tenant_id=run.tenant_id,
            type=EventType.WORKFLOW_STEP,
            severity=Severity.HIGH if step.status == RunStatus.FAILED else Severity.INFO,
            entities=list(run.entity_ids),
            ts=utc_now(),
            confidence=1.0,
            explanation=explanation.build(),
            rule_id=f"workflow.{playbook.name}",
            attributes={
                "run_id": run.run_id,
                "playbook": playbook.name,
                "step_id": step.step_id,
                "step_name": step.name,
                "status": str(step.status),
                "attempts": step.attempts,
                "progress": round(run.progress, 3),
                "output": step.output,
            },
        )
        await self._emit(event, ctx)

    async def _publish_summary(
        self, outcome: RunOutcome, playbook: Playbook, ctx: MessageContext | None
    ) -> None:
        run = outcome.run
        summary = (
            f"{playbook.name} completed all {playbook.step_count} steps"
            if outcome.ok
            else f"{playbook.name} failed and rolled back {len(outcome.compensated)} step(s)"
        )
        explanation = ExplanationBuilder(summary=summary)
        explanation.add_rule(f"workflow.{playbook.name}")
        for step in run.steps:
            explanation.add_timeline(
                step.finished_ts or utc_now(),
                "action",
                f"{step.name}: {step.status}" + (f" ({step.error})" if step.error else ""),
                ref=step.step_id,
            )
        for undone in outcome.compensated:
            explanation.add_note(f"rolled back: {undone}")
        for failure in outcome.compensation_failures:
            explanation.add_note(f"ROLLBACK FAILED: {failure}")
        if outcome.needs_human:
            explanation.degraded(
                "a compensation failed, so the world is in a state nobody chose — this needs a human"
            )

        await self._emit(
            Event(
                tenant_id=run.tenant_id,
                type=EventType.WORKFLOW_STEP,
                severity=Severity.CRITICAL
                if outcome.needs_human
                else (Severity.HIGH if not outcome.ok else Severity.INFO),
                entities=list(run.entity_ids),
                ts=utc_now(),
                confidence=1.0,
                explanation=explanation.build(),
                rule_id=f"workflow.{playbook.name}",
                attributes={
                    "run_id": run.run_id,
                    "playbook": playbook.name,
                    "status": str(run.status),
                    "progress": round(run.progress, 3),
                    "compensated": outcome.compensated,
                    "compensation_failures": outcome.compensation_failures,
                    "final": True,
                },
            ),
            ctx,
        )

    async def _emit(self, event: Event, ctx: MessageContext | None) -> None:
        if ctx is not None:
            await ctx.publish(Topic.EVENTS, event)
        else:
            await self.publish(Topic.EVENTS, event)

    async def _persist(self, run: WorkflowRun) -> None:
        """Upsert the run, steps and all.

        Upsert rather than append because a run's *current* state is what a UI reads, and the step history
        is inside the payload. The append-only record of what happened is the event stream, which is a
        different table with a trigger enforcing it.
        """
        await self.pool.execute(
            """
            INSERT INTO workflow_runs (
                tenant_id, run_id, playbook, status, trigger_event, runner, external_id,
                started_ts, finished_ts, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (tenant_id, run_id) DO UPDATE SET
                status      = EXCLUDED.status,
                finished_ts = EXCLUDED.finished_ts,
                payload     = EXCLUDED.payload
            """,
            (
                run.tenant_id,
                run.run_id,
                run.playbook,
                str(run.status),
                run.trigger_event,
                run.runner,
                run.external_id,
                run.started_ts,
                run.finished_ts,
                run.to_json(),
            ),
        )

    def _explain(self, playbook: Playbook, outcome: RunOutcome, event: Event) -> Any:
        builder = ExplanationBuilder(
            summary=f"{playbook.name} ran in response to {event.type} in {event.zone_id or 'the yard'}"
        )
        builder.add_rule(f"workflow.{playbook.name}", note=playbook.description)
        builder.add_event(event)
        for step in outcome.run.steps:
            builder.add_note(
                f"{step.name}: {step.status}"
                + (f" after {step.attempts} attempt(s)" if step.attempts > 1 else "")
                + (f" — {step.error}" if step.error else "")
            )
        if outcome.compensated:
            builder.add_note(f"rolled back in reverse order: {', '.join(outcome.compensated)}")
        if outcome.needs_human:
            builder.degraded("a compensation failed; the world is in a state nobody chose")
        builder.confidence(0.95 if outcome.ok else 0.5)
        return builder.build()

    # -------------------------------------------------------------------- routes
    def routes(self, app: FastAPI) -> None:
        @app.get("/workflow/vocabulary", tags=["workflow"])
        async def vocabulary() -> dict[str, Any]:
            """Everything an editor needs to offer valid choices.

            Served rather than hard-coded into the UI, because a hard-coded activity list is a UI that offers
            steps the engine cannot run — the failure being a workflow that validates in the browser and is
            rejected on save. The editor should not be able to construct something invalid in the first place.
            """
            from .activities import ACTIVITIES

            return {
                "activities": sorted(ACTIVITIES),
                "operators": list(OPERATORS),
                "severities": list(SEVERITIES),
                "fields": [
                    # The paths that actually exist on a fact, because "type a field path" is how somebody ends
                    # up with a condition that silently never matches.
                    "type",
                    "severity",
                    "zone_id",
                    "payload.zone_id",
                    "payload.confidence",
                    "payload.entity_id",
                    "payload.value",
                ],
                "note": (
                    "Adding a new activity is a code change, deliberately: dispatching a drone is code, and a "
                    "builder that pretended otherwise would end up with a JSON file containing a Python "
                    "expression."
                ),
            }

        @app.post("/workflow/authored/validate", tags=["workflow"])
        async def validate_authored(document: dict[str, Any]) -> dict[str, Any]:
            """Check a workflow without saving it. The endpoint the editor calls on every keystroke.

            Returns EVERY problem, not the first: somebody fixing a workflow wants the list, and a validator
            revealing one problem per attempt turns a five-minute task into twenty.
            """
            spec = parse(document)
            problems = validate(spec)
            result: dict[str, Any] = {
                "valid": not problems,
                "problems": [problem.describe() for problem in problems],
            }
            if not problems:
                playbook = to_playbook(spec)
                # The execution ORDER, which is the one thing an author cannot read off their own JSON: the DAG
                # is topologically sorted, so the steps may not run in the order they were written.
                result["execution_order"] = [step.step_id for step in playbook.steps]
                result["compensation_order"] = [
                    step.step_id for step in reversed(playbook.steps) if step.compensate
                ]
            return result

        @app.get("/workflow/authored", tags=["workflow"])
        async def list_authored() -> dict[str, Any]:
            return {
                "workflows": [spec.describe() for spec in self._authored.values()],
                # The rejected ones travel with the valid ones. A workflow that failed to load is otherwise
                # indistinguishable from one that never fires, and the author will assume the latter.
                "rejected": [problem.describe() for problem in self._authored_problems],
            }

        @app.put("/workflow/authored/{name}", tags=["workflow"])
        async def save_authored(name: str, document: dict[str, Any]) -> dict[str, Any]:
            """Save a workflow, refusing an invalid one.

            Refusing rather than saving-and-warning. A saved-but-broken workflow is the worst of both: it appears
            in the list, an operator believes it is armed, and it never runs.
            """
            document = {**document, "name": name}
            spec = parse(document)
            problems = validate(spec)
            if problems:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": f"{len(problems)} problem(s); nothing was saved",
                        "problems": [problem.describe() for problem in problems],
                    },
                )
            if name in PLAYBOOKS:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{name!r} is a code playbook. Authored files must not be able to replace one — that "
                        "would be a way to disable the fire response without anybody noticing."
                    ),
                )
            self.workflow_dir.mkdir(parents=True, exist_ok=True)
            path = self.workflow_dir / f"{name}.json"
            path.write_text(json.dumps(spec.describe(), indent=2) + "\n")
            self._reload_authored()
            playbook = to_playbook(spec)
            self.log.info(
                "workflow.authored_saved",
                workflow=name,
                steps=playbook.step_count,
                triggers=list(spec.event_types),
            )
            return {
                "saved": name,
                "path": str(path),
                "execution_order": [step.step_id for step in playbook.steps],
                "armed": spec.enabled,
            }

        @app.delete("/workflow/authored/{name}", tags=["workflow"])
        async def delete_authored(name: str) -> dict[str, Any]:
            path = self.workflow_dir / f"{name}.json"
            if not path.exists():
                raise HTTPException(
                    status_code=404,
                    detail=f"no authored workflow {name!r}; have {sorted(self._authored)}",
                )
            path.unlink()
            self._reload_authored()
            return {"deleted": name}

        @app.get("/workflow/playbooks", tags=["workflow"])
        async def playbooks() -> dict[str, Any]:
            """Every playbook, its triggers, and its steps with their compensations."""
            return {"playbooks": [playbook.describe() for playbook in PLAYBOOKS.values()]}

        @app.get("/workflow/runs", tags=["workflow"])
        async def runs(limit: int = 20) -> dict[str, Any]:
            """Recent runs with per-step status — what the UI renders as live progress."""
            return self.ledger.describe(limit=limit)

        @app.get("/workflow/runs/{run_id}", tags=["workflow"])
        async def run_detail(run_id: str) -> dict[str, Any]:
            run = self.ledger.get(run_id)
            if run is None:
                row = await self.pool.fetchrow(
                    "SELECT payload FROM workflow_runs WHERE tenant_id = %s AND run_id = %s",
                    (self.settings.tenant_id, run_id),
                )
                if row is None:
                    raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
                return dict(row["payload"])
            return run.to_wire()

        @app.post("/workflow/run/{playbook_name}", tags=["workflow"])
        async def run_now(playbook_name: str, zone_id: str = "dock_3") -> dict[str, Any]:
            """Start a playbook by hand.

            The demo path, and the way to see a five-step response without waiting for a real fire. It
            builds a synthetic trigger event and says so in the run's record, so a run started by a human
            is never mistaken for one started by a detection.
            """
            playbook = PLAYBOOKS.get(playbook_name)
            if playbook is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"unknown playbook {playbook_name!r}; have {sorted(PLAYBOOKS)}",
                )
            trigger = Event(
                tenant_id=self.settings.tenant_id,
                type=EventType.FIRE_DETECTED,
                severity=Severity.CRITICAL,
                zone_id=zone_id,
                confidence=1.0,
                rule_id="workflow.manual_trigger",
                attributes={"manual": True, "note": "started by hand, not by a detection"},
            )
            outcome = await self._run(playbook, trigger, None)
            return {
                "run_id": outcome.run.run_id,
                "playbook": playbook.name,
                "status": str(outcome.run.status),
                "progress": round(outcome.run.progress, 3),
                "steps": [
                    {
                        "name": step.name,
                        "status": str(step.status),
                        "attempts": step.attempts,
                        "output": step.output,
                        "error": step.error,
                    }
                    for step in outcome.run.steps
                ],
                "compensated": outcome.compensated,
                "compensation_failures": outcome.compensation_failures,
                "needs_human": outcome.needs_human,
            }


__all__ = ["WorkflowService"]
