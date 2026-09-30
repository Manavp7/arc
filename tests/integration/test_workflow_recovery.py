"""Persisted admission, ownership and crash recovery in disposable SQL schemas."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from sio_workflow.activities import ActivityContext
from sio_workflow.durable import DurableRunner, WorkflowStore
from sio_workflow.playbooks import Playbook, StepSpec

from sio_core import PgPool
from sio_core.config import Settings
from sio_schemas import Event

pytestmark = pytest.mark.infra
MIGRATION = Path(__file__).resolve().parents[2] / "infra/postgres/011_workflow_recovery.sql"


@pytest.fixture
async def database(tmp_path):
    cfg = Settings(data_dir=tmp_path, tenant_id="workflow-recovery-test")
    schema = f"workflow_recovery_{uuid4().hex}"
    control = await psycopg.AsyncConnection.connect(cfg.pg_dsn, autocommit=True)
    pool = PgPool(
        make_conninfo(cfg.pg_dsn, options=f"-c search_path={schema},public"), min_size=2, max_size=6
    )
    try:
        await control.execute(f"CREATE SCHEMA {schema}")
        await pool.open()
        await pool.execute("CREATE TABLE workflow_runs (LIKE public.workflow_runs INCLUDING ALL)")
        await pool.execute(await asyncio.to_thread(MIGRATION.read_text))
        yield cfg, pool, WorkflowStore(pool)
    finally:
        await pool.close()
        await control.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await control.close()


def event(tenant):
    return Event(
        tenant_id=tenant, type="fire_detected", severity="critical", zone_id="yard", confidence=0.9
    )


def context(tenant, run_id):
    return ActivityContext(
        api_url="http://unused", ingest_url="http://unused", tenant_id=tenant, run_id=run_id
    )


async def test_concurrent_admission_and_restart_preserve_cooldown_and_receipts(database):
    cfg, pool, store = database
    playbook = Playbook("Authored", "Test", (), cooldown_s=600)
    first, second = event(cfg.tenant_id), event(cfg.tenant_id)
    identifiers = await asyncio.gather(
        store.enqueue(playbook, first, "yard", dry_run=True),
        WorkflowStore(pool).enqueue(playbook, second, "yard", dry_run=True),
    )
    assert sum(identifier is not None for identifier in identifiers) == 1
    assert await pool.fetchval("SELECT count(*) FROM workflow_executions") == 1
    assert await pool.fetchval("SELECT count(*) FROM workflow_trigger_receipts") == 2
    for original, identifier in zip((first, second), identifiers, strict=True):
        assert (
            await WorkflowStore(pool).enqueue(playbook, original, "yard", dry_run=False)
            == identifier
        )
    # Even after cooldown expires, a suppressed event replay cannot start later.
    await pool.execute("UPDATE workflow_cooldowns SET until_at = now()-interval '1 second'")
    suppressed = first if identifiers[0] is None else second
    assert await store.enqueue(playbook, suppressed, "yard", dry_run=True) is None


async def test_only_one_live_executor_claims_a_run(database):
    cfg, pool, store = database
    identifier = await store.enqueue(
        Playbook("Authored", "Test", ()), event(cfg.tenant_id), "yard", dry_run=True
    )
    async with store.claim(cfg.tenant_id, identifier) as claimed:
        assert claimed is not None
        async with WorkflowStore(pool).claim(cfg.tenant_id, identifier) as other:
            assert other is None
    async with WorkflowStore(pool).claim(cfg.tenant_id, identifier) as restarted:
        assert restarted is not None


async def test_fresh_store_resumes_checkpoint_without_repeating_completed_effect(database):
    cfg, pool, store = database
    calls = []

    async def completed(ctx, key):
        calls.append(key)
        return {"retained": "output"}

    async def interrupted(ctx, key):
        calls.append(key)
        raise asyncio.CancelledError

    completed.recovery_safe = interrupted.recovery_safe = True
    playbook = Playbook(
        "Authored",
        "Frozen",
        (StepSpec("first", "First", "first"), StepSpec("second", "Second", "second", backoff_s=0)),
    )
    identifier = await store.enqueue(playbook, event(cfg.tenant_id), "yard", dry_run=True)
    with pytest.raises(asyncio.CancelledError):
        async with store.claim(cfg.tenant_id, identifier) as execution:
            await DurableRunner({"first": completed, "second": interrupted}).execute(
                execution, context(cfg.tenant_id, identifier)
            )

    async def recovered(ctx, key):
        calls.append(key)
        return {"recovered": True}

    recovered.recovery_safe = True
    fresh = WorkflowStore(pool)
    assert identifier in await fresh.recoverable(cfg.tenant_id)
    async with fresh.claim(cfg.tenant_id, identifier) as execution:
        result = await DurableRunner({"first": completed, "second": recovered}).execute(
            execution, context(cfg.tenant_id, identifier)
        )
    assert result.ok and calls == ["first", "second", "second"]
    assert result.run.steps[0].output == {"retained": "output"}
    assert identifier not in await fresh.recoverable(cfg.tenant_id)
    page = await fresh.summary(cfg.tenant_id)
    assert page["recent"][0]["status"] == "completed"
    assert not (await fresh.summary(cfg.tenant_id, allowed_zones=("other",)))["recent"]


async def test_interrupted_external_effect_becomes_persisted_manual_hold(database):
    cfg, pool, store = database
    calls = []

    async def external(ctx, key):
        calls.append(key)
        raise asyncio.CancelledError

    playbook = Playbook("External", "Test", (StepSpec("send", "Send", "external"),))
    identifier = await store.enqueue(playbook, event(cfg.tenant_id), "yard", dry_run=False)
    with pytest.raises(asyncio.CancelledError):
        async with store.claim(cfg.tenant_id, identifier) as execution:
            await DurableRunner({"external": external}).execute(
                execution, context(cfg.tenant_id, identifier)
            )
    async with WorkflowStore(pool).claim(cfg.tenant_id, identifier) as execution:
        result = await DurableRunner({"external": external}).execute(
            execution, context(cfg.tenant_id, identifier)
        )
    assert result.needs_human and calls == ["send"]
    assert (
        await pool.fetchval("SELECT state FROM workflow_executions WHERE run_id=%s", (identifier,))
        == "needs_human"
    )
    assert identifier not in await store.recoverable(cfg.tenant_id)
    page = await store.summary(cfg.tenant_id)
    assert page["recent"][0]["needs_human"] is True
