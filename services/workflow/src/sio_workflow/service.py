"""Workflow service: events in, response playbooks out, progress visible (PRD M15)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request

from sio_core import MessageContext, PgPool, SioService, get_pg_pool
from sio_core.authn import ServiceIdentity
from sio_core.explain import ExplanationBuilder
from sio_core.guard import principal_of
from sio_core.source_scope import zone_visible, zones_for
from sio_core.tenancy import current_tenant
from sio_schemas import (
    BusMessage,
    Event,
    EventType,
    RunStatus,
    Severity,
    Topic,
    WorkflowRun,
    WorkflowStep,
    utc_now,
)

from .activities import ActivityContext
from .durable import DurableRunner, WorkflowStore, frozen_playbook
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
from .runner import RunLedger, RunOutcome


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
        if self.settings.workflow_runner != "inline":
            raise ValueError(
                "SIO_WORKFLOW_RUNNER=temporal is not implemented; select inline. "
                "The inline runner persists PostgreSQL checkpoints."
            )
        self.runner = DurableRunner()
        self.durable = WorkflowStore(self.pool)
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
        self._needing_human = int(
            await self.pool.fetchval(
                "SELECT count(*) FROM workflow_executions WHERE tenant_id=%s AND state='needs_human'",
                (self.settings.tenant_id,),
            )
            or 0
        )
        if self._needing_human:
            # A failed compensation leaves the world in a state nobody chose. That must not be a log line
            # nobody reads — it degrades health until someone looks.
            checks["compensation"] = (
                f"degraded: {self._needing_human} run(s) require effect reconciliation"
            )
        return checks

    async def health_info(self) -> dict[str, str]:
        suppressed = await self.pool.fetchval(
            "SELECT count(*) FROM workflow_trigger_receipts WHERE tenant_id=%s AND run_id IS NULL",
            (self.settings.tenant_id,),
        )
        return {
            "runs_started": str(self._started),
            "completed": str(self._completed),
            "failed": str(self._failed),
            "suppressed_by_cooldown": str(suppressed or 0),
            "needing_human": str(self._needing_human),
            "runner": self.runner.name,
            "dry_run": str(self.settings.workflow_dry_run).lower(),
            "durability": "PostgreSQL checkpoints; safe steps resume, uncertain effects require reconciliation",
            "action_mode": "dry_run"
            if self.settings.workflow_dry_run
            else "armed; actuators unsupported",
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
        if message.kind != "Event" or message.tenant_id != self.settings.tenant_id:
            return
        event = message.decode(Event)
        if event.rule_id and event.rule_id.startswith("workflow."):
            # A playbook's own progress events come back around on this topic. Reacting to them would have
            # a fire response trigger a fire response.
            return

        for playbook in self._playbooks_for(event, message):
            subject = self._subject(playbook, event)
            identifier = await self.durable.enqueue(
                playbook, event, subject, dry_run=self.settings.workflow_dry_run
            )
            if identifier is None:
                self.log.info(
                    "workflow.suppressed",
                    playbook=playbook.name,
                    subject=subject,
                    why=f"within the {playbook.cooldown_s:.0f}s cooldown",
                )
                continue
            await self._resume(identifier, ctx)

    @staticmethod
    def _subject(playbook: Playbook, event: Event) -> str:
        parts = []
        for key in playbook.key_by:
            if key == "zone_id":
                parts.append(event.zone_id or "")
            elif key == "entity_id":
                parts.append(event.entities[0] if event.entities else "")
        return "|".join(parts) or "*"

    def _report_token(self, tenant_id: str) -> str:
        if self.settings.auth_mode == "dev":
            tenant_settings = self.settings.model_copy(update={"tenant_id": tenant_id})
            return ServiceIdentity("workflow", tenant_settings).token()
        # The API verifies this supplied token; report generation also rejects any tenant mismatch.
        return self.settings.workflow_service_token

    async def _run(
        self, playbook: Playbook, event: Event, ctx: MessageContext | None
    ) -> RunOutcome:
        identifier = await self.durable.enqueue(
            playbook, event, self._subject(playbook, event), dry_run=self.settings.workflow_dry_run
        )
        if identifier is None:
            raise HTTPException(409, "A run for this subject is within its persisted cooldown.")
        outcome = await self._resume(identifier, ctx)
        if outcome is None:
            raise HTTPException(
                409, "This run is already executing or has finished; read its saved status."
            )
        return outcome

    async def _resume(
        self, identifier: str, ctx: MessageContext | None = None
    ) -> RunOutcome | None:
        async with self.durable.claim(self.settings.tenant_id, identifier) as execution:
            if execution is None:
                return None
            playbook = frozen_playbook(execution.record)
            saved = execution.record["context"]
            event = Event.model_validate(saved["trigger"])
            context = ActivityContext(
                api_url=f"http://127.0.0.1:{self.settings.api_port}",
                ingest_url=f"http://127.0.0.1:{self.settings.ingest_port}",
                tenant_id=saved["tenant_id"],
                run_id=identifier,
                trigger_event_id=saved["trigger_event_id"],
                zone_id=saved["zone_id"],
                entity_ids=saved["entity_ids"],
                dry_run=saved["dry_run"],
                bearer_token=self._report_token(saved["tenant_id"]),
            )
            self._started += 1

            async def on_progress(run: WorkflowRun, step: WorkflowStep) -> None:
                await self._publish_step(run, step, playbook, ctx, zone_id=context.zone_id)

            try:
                outcome = await self.runner.execute(execution, context, on_progress)
            finally:
                await context.close()
            recovery_notes = list(outcome.run.explanation.notes)
            outcome.run.explanation = self._explain(playbook, outcome, event)
            outcome.run.explanation.notes.extend(recovery_notes)
            await execution.save(execution.record["state"])
            self.ledger.record(outcome.run)
            if outcome.ok:
                self._completed += 1
            else:
                self._failed += 1
            if outcome.needs_human:
                self._needing_human += 1
            await self._publish_summary(outcome, playbook, ctx, zone_id=context.zone_id)
            return outcome

    async def tick(self) -> None:
        for identifier in await self.durable.recoverable(self.settings.tenant_id):
            await self._resume(identifier)
        self._needing_human = int(
            await self.pool.fetchval(
                "SELECT count(*) FROM workflow_executions WHERE tenant_id=%s AND state='needs_human'",
                (self.settings.tenant_id,),
            )
            or 0
        )

    # ---------------------------------------------------------------- publishing
    async def _publish_step(
        self,
        run: WorkflowRun,
        step: WorkflowStep,
        playbook: Playbook,
        ctx: MessageContext | None,
        *,
        zone_id: str | None = None,
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
            zone_id=zone_id,
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
        self,
        outcome: RunOutcome,
        playbook: Playbook,
        ctx: MessageContext | None,
        *,
        zone_id: str | None = None,
    ) -> None:
        run = outcome.run
        summary = (
            f"{playbook.name} completed; "
            f"{sum(step.status == RunStatus.FAILED for step in run.steps)} optional step(s) failed"
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
        for uncertainty in outcome.uncertain_effects:
            explanation.add_note(f"EFFECT UNCERTAIN: {uncertainty}")
        if outcome.needs_human:
            explanation.degraded(
                "A failed compensation or uncertain effect requires human reconciliation"
            )

        await self._emit(
            Event(
                tenant_id=run.tenant_id,
                zone_id=zone_id,
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
                    "uncertain_effects": outcome.uncertain_effects,
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
            builder.degraded(
                "A failed compensation or uncertain effect requires human reconciliation"
            )
        builder.confidence(0.95 if outcome.ok else 0.5)
        return builder.build()

    def _require_configuration_tenant(self) -> None:
        if current_tenant() != self.settings.tenant_id:
            raise HTTPException(
                status_code=403, detail="workflow configuration belongs to another tenant"
            )

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
            self._require_configuration_tenant()
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
            self._require_configuration_tenant()
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
            self._require_configuration_tenant()
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
        async def runs(request: Request, limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
            """Persisted runs remain visible after restart."""
            return await self.durable.summary(
                current_tenant(), limit, allowed_zones=zones_for(principal_of(request))
            )

        @app.get("/workflow/runs/{run_id}", tags=["workflow"])
        async def run_detail(run_id: str, request: Request) -> dict[str, Any]:
            row = await self.pool.fetchrow(
                "SELECT r.payload,e.state,e.context FROM workflow_runs r LEFT JOIN workflow_executions e USING (tenant_id,run_id) WHERE r.tenant_id=%s AND r.run_id=%s",
                (current_tenant(), run_id),
            )
            if row is None or not zone_visible(
                zones_for(principal_of(request)), (row.get("context") or {}).get("zone_id")
            ):
                raise HTTPException(404, "Workflow run not found")
            return {
                **row["payload"],
                "recovery_state": row["state"],
                "needs_human": row["state"] == "needs_human",
            }

        @app.post("/workflow/run/{playbook_name}", tags=["workflow"])
        async def run_now(
            playbook_name: str, request: Request, zone_id: str = "dock_3"
        ) -> dict[str, Any]:
            """Start a playbook by hand.

            The demo path, and the way to see a five-step response without waiting for a real fire. It
            builds a synthetic trigger event and says so in the run's record, so a run started by a human
            is never mistaken for one started by a detection.
            """
            if not zone_visible(zones_for(principal_of(request)), zone_id):
                raise HTTPException(403, "Your zone access does not permit this workflow")
            playbook = PLAYBOOKS.get(playbook_name)
            if playbook is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"unknown playbook {playbook_name!r}; have {sorted(PLAYBOOKS)}",
                )
            trigger = Event(
                tenant_id=current_tenant(),
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
                "uncertain_effects": outcome.uncertain_effects,
                "needs_human": outcome.needs_human,
            }


__all__ = ["WorkflowService"]
