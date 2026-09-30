"""PostgreSQL checkpoints with explicit handling of uncertain effects.

A session advisory lock gives a live executor exclusive ownership until its
connection closes. Each attempt is persisted before invocation. Recovery skips
finished steps and only repeats activities explicitly declared safe to repeat;
unknown in-flight effects stop for human reconciliation. No exactly-once claim
is made for external systems.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sio_core import PgPool
from sio_schemas import Event, RunStatus, WorkflowRun, WorkflowStep, utc_now

from .activities import ACTIVITIES, ActivityContext
from .playbooks import Playbook, StepSpec
from .runner import ProgressHook, RunOutcome


async def row(connection: Any, sql: str, params: Any = None) -> dict[str, Any] | None:
    async with PgPool.dict_cursor(connection) as cursor:
        await cursor.execute(sql, params)
        return await cursor.fetchone()


class Execution:
    def __init__(self, connection: Any, record: dict[str, Any]) -> None:
        self.connection = connection
        self.record = record
        self.checkpoint = record["checkpoint"]
        self.run = WorkflowRun.model_validate(self.checkpoint["run"])

    async def save(self, state: str) -> None:
        self.checkpoint["run"] = self.run.to_wire()
        async with self.connection.transaction():
            await self.connection.execute(
                "UPDATE workflow_executions SET checkpoint = %s::jsonb, state = %s, updated_at = now() "
                "WHERE tenant_id = %s AND run_id = %s",
                (json.dumps(self.checkpoint), state, self.run.tenant_id, self.run.run_id),
            )
            await self.connection.execute(
                "INSERT INTO workflow_runs (tenant_id, run_id, playbook, status, trigger_event, runner, started_ts, finished_ts, payload) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT (tenant_id, run_id) DO UPDATE "
                "SET status=EXCLUDED.status, finished_ts=EXCLUDED.finished_ts, payload=EXCLUDED.payload",
                (
                    self.run.tenant_id,
                    self.run.run_id,
                    self.run.playbook,
                    str(self.run.status),
                    self.run.trigger_event,
                    self.run.runner,
                    self.run.started_ts,
                    self.run.finished_ts,
                    self.run.to_json(),
                ),
            )
        self.record["state"] = state


class WorkflowStore:
    def __init__(self, pool: PgPool) -> None:
        self.pool = pool

    async def enqueue(
        self, playbook: Playbook, event: Event, subject: str, *, dry_run: bool
    ) -> str | None:
        run_id = (
            "wfr_"
            + uuid5(NAMESPACE_URL, json.dumps([event.tenant_id, playbook.name, event.event_id])).hex
        )
        async with await self.pool._conn() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (json.dumps(["workflow-start", event.tenant_id, playbook.name, subject]),),
            )
            existing = await row(
                conn,
                "SELECT run_id FROM workflow_trigger_receipts WHERE tenant_id=%s AND playbook=%s AND event_id=%s",
                (event.tenant_id, playbook.name, event.event_id),
            )
            if existing is not None:
                return existing["run_id"]
            permitted = await row(
                conn,
                "INSERT INTO workflow_cooldowns (tenant_id,playbook,subject,until_at) VALUES (%s,%s,%s,now()+%s*interval '1 second') "
                "ON CONFLICT (tenant_id,playbook,subject) DO UPDATE SET until_at=EXCLUDED.until_at "
                "WHERE workflow_cooldowns.until_at <= now() RETURNING subject",
                (event.tenant_id, playbook.name, subject, max(0, playbook.cooldown_s)),
            )
            await conn.execute(
                "INSERT INTO workflow_trigger_receipts (tenant_id,playbook,event_id,run_id) VALUES (%s,%s,%s,%s)",
                (event.tenant_id, playbook.name, event.event_id, run_id if permitted else None),
            )
            if not permitted:
                return None
            run = WorkflowRun(
                run_id=run_id,
                tenant_id=event.tenant_id,
                playbook=playbook.name,
                status=RunStatus.PENDING,
                trigger_event=event.event_id,
                runner="inline",
                entity_ids=list(event.entities),
                steps=[WorkflowStep(step_id=s.step_id, name=s.name) for s in playbook.steps],
            )
            checkpoint = {
                "run": run.to_wire(),
                "inflight": None,
                "compensated": [],
                "compensation_failures": [],
                "uncertain_effects": [],
                "needs_human": False,
            }
            context = {
                "tenant_id": event.tenant_id,
                "run_id": run_id,
                "trigger_event_id": event.event_id,
                "zone_id": event.zone_id,
                "entity_ids": list(event.entities),
                "dry_run": dry_run,
                "trigger": event.to_wire(),
            }
            await conn.execute(
                "INSERT INTO workflow_executions (tenant_id,run_id,playbook,subject,trigger_event,definition,context,checkpoint) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)",
                (
                    event.tenant_id,
                    run_id,
                    playbook.name,
                    subject,
                    event.event_id,
                    json.dumps(asdict(playbook)),
                    json.dumps(context),
                    json.dumps(checkpoint),
                ),
            )
            await Execution(conn, {"checkpoint": checkpoint}).save("queued")
        return run_id

    @asynccontextmanager
    async def claim(self, tenant: str, run_id: str) -> AsyncIterator[Execution | None]:
        key = json.dumps(["workflow-run", tenant, run_id])
        async with await self.pool._conn() as conn:
            claimed = await row(
                conn, "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS claimed", (key,)
            )
            if not claimed or not claimed["claimed"]:
                yield None
                return
            try:
                record = await row(
                    conn,
                    "SELECT * FROM workflow_executions WHERE tenant_id=%s AND run_id=%s",
                    (tenant, run_id),
                )
                yield (
                    Execution(conn, record)
                    if record and record["state"] in {"queued", "running", "compensating"}
                    else None
                )
            finally:
                # A dead connection releases its session lock on PostgreSQL itself.
                try:
                    await conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))
                except BaseException:
                    # Never return a possibly locked session to the connection pool.
                    await conn.close()
                    raise

    async def recoverable(self, tenant: str, limit: int = 50) -> list[str]:
        rows = await self.pool.fetch(
            "SELECT run_id FROM workflow_executions WHERE tenant_id=%s AND state IN ('queued','running','compensating') ORDER BY updated_at LIMIT %s",
            (tenant, limit),
        )
        return [item["run_id"] for item in rows]

    async def summary(
        self, tenant: str, limit: int = 20, *, allowed_zones: tuple[str, ...] | None = None
    ) -> dict[str, Any]:
        rows = await self.pool.fetch(
            "SELECT r.payload, e.state AS recovery_state FROM workflow_runs r LEFT JOIN workflow_executions e USING (tenant_id,run_id) "
            "WHERE r.tenant_id=%s AND (%s::boolean OR e.context->>'zone_id' = ANY(%s::text[])) ORDER BY r.started_ts DESC LIMIT %s",
            (tenant, allowed_zones is None, list(allowed_zones or ()), limit),
        )
        runs = [WorkflowRun.model_validate(item["payload"]) for item in rows]
        counts = await self.pool.fetch(
            "SELECT r.playbook, count(*) AS n FROM workflow_runs r LEFT JOIN workflow_executions e USING (tenant_id,run_id) WHERE r.tenant_id=%s AND (%s::boolean OR e.context->>'zone_id' = ANY(%s::text[])) GROUP BY r.playbook",
            (tenant, allowed_zones is None, list(allowed_zones or ())),
        )
        suppressed = (
            await self.pool.fetchval(
                "SELECT count(*) FROM workflow_trigger_receipts WHERE tenant_id=%s AND run_id IS NULL",
                (tenant,),
            )
            if allowed_zones is None
            else None
        )
        return {
            "runs": sum(item["n"] for item in counts),
            "suppressed_by_cooldown": suppressed,
            "by_playbook": {item["playbook"]: item["n"] for item in counts},
            "recent": [
                {
                    "run_id": run.run_id,
                    "playbook": run.playbook,
                    "status": str(run.status),
                    "progress": round(run.progress, 2),
                    "started": run.started_ts.isoformat(),
                    "recovery_state": item["recovery_state"],
                    "needs_human": item["recovery_state"] == "needs_human",
                    "steps": [
                        {"name": step.name, "status": str(step.status), "attempts": step.attempts}
                        for step in run.steps
                    ],
                }
                for run, item in zip(runs, rows, strict=True)
            ],
        }


def frozen_playbook(record: dict[str, Any]) -> Playbook:
    value = dict(record["definition"])
    value["steps"] = tuple(StepSpec(**spec) for spec in value["steps"])
    value["trigger_event_types"] = tuple(value["trigger_event_types"])
    value["key_by"] = tuple(value["key_by"])
    return Playbook(**value)


class DurableRunner:
    name = "inline"

    def __init__(self, activities: dict[str, Any] | None = None) -> None:
        self.activities = ACTIVITIES if activities is None else activities

    async def execute(
        self,
        execution: Execution,
        context: ActivityContext,
        on_progress: ProgressHook | None = None,
    ) -> RunOutcome:
        playbook = frozen_playbook(execution.record)
        run, state = execution.run, execution.checkpoint
        state.setdefault("uncertain_effects", [])
        outcome = RunOutcome(
            run, state["compensated"], state["compensation_failures"], state["uncertain_effects"]
        )
        inflight = state.get("inflight")
        if inflight and not self._safe(inflight["activity"]):
            await self._uncertain(
                execution,
                outcome,
                f"Interrupted activity {inflight['activity']} may already have acted; reconcile before any retry.",
            )
            return outcome
        run.status = RunStatus.RUNNING
        if execution.record["state"] != "compensating":
            await execution.save("running")
            for index, spec in enumerate(playbook.steps):
                step = run.steps[index]
                if step.status == RunStatus.COMPLETED or (
                    step.status == RunStatus.FAILED and spec.optional
                ):
                    continue
                if step.status == RunStatus.FAILED:
                    await execution.save("compensating")
                    break
                ok = await self._attempt(execution, context, spec, step)
                if state["needs_human"]:
                    return RunOutcome(
                        run,
                        state["compensated"],
                        state["compensation_failures"],
                        state["uncertain_effects"],
                    )
                await self._progress(on_progress, run, step)
                if not ok and not spec.optional:
                    await execution.save("compensating")
                    break
            else:
                run.status, run.finished_ts = RunStatus.COMPLETED, utc_now()
                await execution.save("completed")
                return outcome
        for spec, step in reversed(list(zip(playbook.steps, run.steps, strict=True))):
            if step.status != RunStatus.COMPLETED or not spec.compensate:
                continue
            await self._attempt(execution, context, spec, step, compensate=True)
            if state["needs_human"]:
                return RunOutcome(
                    run,
                    state["compensated"],
                    state["compensation_failures"],
                    state["uncertain_effects"],
                )
            await execution.save("compensating")
            await self._progress(on_progress, run, step)
        run.status, run.finished_ts = RunStatus.FAILED, utc_now()
        await execution.save("needs_human" if outcome.needs_human else "failed")
        return outcome

    def _safe(self, name: str) -> bool:
        return bool(getattr(self.activities.get(name), "recovery_safe", False))

    async def _uncertain(self, execution: Execution, outcome: RunOutcome, reason: str) -> None:
        execution.checkpoint["needs_human"] = True
        inflight = execution.checkpoint.get("inflight") or {}
        identifier = str(inflight.get("step_id", "")).removesuffix(":compensate")
        for step in outcome.run.steps:
            if step.step_id == identifier:
                step.status, step.error = RunStatus.FAILED, reason
        outcome.uncertain_effects.append(reason)
        outcome.run.status, outcome.run.finished_ts = RunStatus.FAILED, utc_now()
        outcome.run.explanation.notes.append(reason)
        await execution.save("needs_human")

    async def _attempt(
        self,
        execution: Execution,
        context: ActivityContext,
        spec: StepSpec,
        step: WorkflowStep,
        *,
        compensate: bool = False,
    ) -> bool:
        name = spec.compensate if compensate else spec.activity
        activity = self.activities.get(name)
        state = execution.checkpoint
        phase = "compensating" if compensate else "running"
        key = f"{spec.step_id}:compensate" if compensate else spec.step_id
        if activity is None:
            step.error = f"Activity {name!r} is unavailable; no effect attempted."
            step.status = RunStatus.FAILED
            state["inflight"] = None
            if compensate:
                state["compensation_failures"].append(f"{spec.step_id}: {step.error}")
            await execution.save(phase)
            return False
        attempts = state.setdefault("attempts", {})
        maximum = 1 if compensate else spec.max_attempts
        # An interrupted safe invocation consumed an attempt too. Exhaustion is explicit.
        while attempts.get(key, 0) < maximum:
            attempts[key] = attempts.get(key, 0) + 1
            if not compensate:
                step.status = RunStatus.RUNNING
                step.started_ts = step.started_ts or utc_now()
                step.attempts = attempts[key]
            state["inflight"] = {"step_id": key, "activity": name}
            await execution.save(phase)  # failure propagates; NEVER act without a checkpoint
            try:
                result = await asyncio.wait_for(
                    activity(context, key, **spec.arguments), timeout=spec.timeout_s
                )
                output = result if isinstance(result, dict) else {"result": result}
            except Exception as error:
                if not self._safe(name):
                    await self._uncertain(
                        execution,
                        RunOutcome(
                            execution.run,
                            state["compensated"],
                            state["compensation_failures"],
                            state["uncertain_effects"],
                        ),
                        f"Activity {name} did not confirm its result ({type(error).__name__}); an effect may have occurred. Reconcile manually.",
                    )
                    return False
                step.error = f"Activity {name} failed ({type(error).__name__})."
                state["inflight"] = None
                await execution.save(phase)
                if attempts[key] < maximum:
                    await asyncio.sleep(spec.backoff_s * 2 ** (attempts[key] - 1))
                continue
            if output.get("error") and not self._safe(name):
                if compensate:
                    step.output["compensation"] = output
                else:
                    step.output = output
                await self._uncertain(
                    execution,
                    RunOutcome(
                        execution.run,
                        state["compensated"],
                        state["compensation_failures"],
                        state["uncertain_effects"],
                    ),
                    f"Activity {name} returned an error without a recovery contract; an effect may have occurred. Reconcile manually.",
                )
                return False
            state["inflight"] = None
            step.finished_ts = utc_now()
            step.error = str(output["error"]) if output.get("error") else None
            if compensate:
                step.output["compensation"] = output
                step.status = RunStatus.COMPENSATED if not step.error else RunStatus.FAILED
                if step.error:
                    state["compensation_failures"].append(f"{spec.step_id}: {step.error}")
                else:
                    state["compensated"].append(spec.step_id)
            else:
                step.output = output
                step.status = RunStatus.COMPLETED if not step.error else RunStatus.FAILED
            await execution.save(phase)
            return step.error is None
        state["inflight"] = None
        step.status, step.finished_ts = RunStatus.FAILED, utc_now()
        step.error = step.error or "Retry budget exhausted after an interrupted attempt."
        if compensate:
            state["compensation_failures"].append(f"{spec.step_id}: {step.error}")
        await execution.save(phase)
        return False

    @staticmethod
    async def _progress(hook: ProgressHook | None, run: WorkflowRun, step: WorkflowStep) -> None:
        if hook:
            # Durable checkpoint already succeeded. Bus progress is best effort only.
            with contextlib.suppress(Exception):
                await hook(run, step)
