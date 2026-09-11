"""Real PostgreSQL workbench verification in isolated, rollback-only test tenants.

No default-tenant records or sources are changed. The installed migration is
exercised as-is; these tests neither apply DDL nor disable immutability triggers.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException
from psycopg.errors import RaiseException
from psycopg.rows import dict_row
from sio_api.case_helpers import CaseCreate
from sio_api.cases import Casework
from sio_api.workbench_store import WorkbenchConflict, WorkbenchStore

from sio_core.authn import Principal
from sio_core.config import Settings
from sio_core.stores.pg import PgPool
from sio_core.tenancy import tenant_scope

pytestmark = pytest.mark.infra


class TransactionQueries:
    """Real SQL on the test transaction's connection, matching the pooled adapter interface."""

    def __init__(self, connection):
        self.connection = connection

    async def execute(self, sql, params=None):
        cursor = await self.connection.execute(sql, params)
        return cursor.rowcount

    async def fetch(self, sql, params=None):
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(sql, params)
            return list(await cursor.fetchall())

    async def fetchrow(self, sql, params=None):
        rows = await self.fetch(sql, params)
        return rows[0] if rows else None


@pytest.fixture
async def database():
    pool = PgPool(Settings().pg_dsn, min_size=1, max_size=1, open_timeout_s=5)
    await pool.open()
    try:
        async with pool._pool.connection() as connection:
            async with connection.transaction(force_rollback=True):
                queries = TransactionQueries(connection)
                tenants = [f"workbench-test-{uuid4().hex}" for _ in range(2)]
                for tenant in tenants:
                    await queries.execute(
                        "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)",
                        (tenant, "Isolated workbench integration test; rolled back"),
                    )
                yield queries, tenants
    finally:
        await pool.close()


async def test_postgres_tenant_isolation_and_compare_and_swap(database):
    queries, (tenant, other) = database
    store = WorkbenchStore(queries)
    first = await store.put(
        tenant, "site", "same-id", {"name": "First tenant"}, expected_revision=0
    )
    await store.put(other, "site", "same-id", {"name": "Other tenant"}, expected_revision=0)
    assert first["revision"] == 1
    assert first["created_at"] and first["updated_at"]
    assert (await store.get(other, "site", "same-id"))["name"] == "Other tenant"
    assert [row["name"] for row in await store.list(tenant, "site")] == ["First tenant"]
    assert await store.get(f"absent-{uuid4().hex}", "site", "same-id") is None

    with pytest.raises(WorkbenchConflict):
        await store.put(tenant, "site", "same-id", {"name": "Duplicate"}, expected_revision=0)
    second = await store.put(tenant, "site", "same-id", {"name": "Edited"}, expected_revision=1)
    assert second["revision"] == 2
    with pytest.raises(WorkbenchConflict):
        await store.put(tenant, "site", "same-id", {"name": "Stale edit"}, expected_revision=1)
    with pytest.raises(WorkbenchConflict):
        await store.delete(tenant, "site", "same-id", expected_revision=1)
    assert (await WorkbenchStore(queries).get(tenant, "site", "same-id"))["name"] == "Edited"
    assert (await store.get(other, "site", "same-id"))["revision"] == 1


async def test_postgres_revision_history_is_generated_and_immutable(database):
    queries, (tenant, other) = database
    store = WorkbenchStore(queries)
    await store.put(tenant, "site", "history-proof", {"name": "Original"}, expected_revision=0)
    await store.put(tenant, "site", "history-proof", {"name": "Current"}, expected_revision=1)
    history = await store.history(tenant, "site", "history-proof")
    assert [(row["revision"], row["payload"]["name"]) for row in history] == [
        (2, "Current"),
        (1, "Original"),
    ]
    assert await store.history(other, "site", "history-proof") == []
    statements = [
        "UPDATE workbench_revisions SET payload = '{}'::jsonb WHERE tenant_id = %s AND kind = 'site' AND record_id = 'history-proof'",
        "DELETE FROM workbench_revisions WHERE tenant_id = %s AND kind = 'site' AND record_id = 'history-proof'",
    ]
    for statement in statements:
        with pytest.raises(RaiseException, match="append-only"):
            # A failed statement rolls back only its savepoint, preserving the outer test transaction.
            async with queries.connection.transaction():
                await queries.execute(statement, (tenant,))
    assert await store.history(tenant, "site", "history-proof") == history


async def test_case_keeps_exact_video_analysis_snapshot_after_new_run(database):
    queries, (tenant, other) = database
    store = WorkbenchStore(queries)
    vid, old_id, new_id = "vid_" + uuid4().hex, "ana_" + uuid4().hex, "ana_" + uuid4().hex
    event = {
        "event_id": "ve_" + uuid4().hex,
        "video_id": vid,
        "analysis_id": old_id,
        "zone_id": "test-zone",
        "rule_id": "entry",
        "title": "Authored motion entry",
        "at_s": 2.5,
        "end_s": 2.5,
        "track_id": "region-1",
        "confidence": None,
        "frame_url": f"/api/review/videos/{vid}/frames/{old_id}/5",
    }
    original = {
        "analysis_id": old_id,
        "video_id": vid,
        "status": "completed",
        "events": [event],
        "model": {"mode": "motion", "name": "Test foreground detector"},
        "rules": [{"rule_id": "entry", "threshold_s": 1}],
        "zones": [{"zone_id": "test-zone", "name": "Original zone"}],
    }
    await store.put(tenant, "analysis", old_id, original, expected_revision=0)
    await store.put(
        tenant,
        "video",
        vid,
        {
            "video_id": vid,
            "title": "Authored clip",
            "duration_s": 6,
            "privacy": "full_frame_pixelation",
            "source": "recorded_file",
            "analysis_id": old_id,
        },
        expected_revision=0,
    )
    principal = Principal(
        subject="integration-reviewer", tenant_id=tenant, roles=frozenset({"operator"}), clearance=3
    )
    with tenant_scope(tenant):
        created = await Casework(store, queries).create(
            CaseCreate(video_id=vid, analysis_id=old_id, event_id=event["event_id"]), principal
        )
    await store.put(
        tenant,
        "analysis",
        new_id,
        {
            "analysis_id": new_id,
            "video_id": vid,
            "status": "completed",
            "events": [],
            "model": {"mode": "onnx", "name": "Different model"},
            "rules": [],
            "zones": [],
        },
        expected_revision=0,
    )
    current_video = await store.get(tenant, "video", vid)
    await store.put(
        tenant,
        "video",
        vid,
        {**current_video, "analysis_id": new_id},
        expected_revision=current_video["revision"],
    )
    # Reconstruct readers so this verifies PostgreSQL rows, not retained service objects.
    fresh_store = WorkbenchStore(queries)
    with tenant_scope(tenant):
        restored = await Casework(fresh_store, queries).detail(created["case_id"], principal)
    assert restored["analysis_id"] == old_id
    assert restored["evidence"] == created["evidence"]
    assert restored["evidence"]["event"] == event
    assert restored["evidence"]["model"] == original["model"]
    assert (await fresh_store.get(tenant, "video", vid))["analysis_id"] == new_id
    assert (await fresh_store.get(tenant, "analysis", old_id))["events"] == [event]
    assert (await fresh_store.history(tenant, "case", created["case_id"]))[0]["payload"][
        "evidence"
    ] == created["evidence"]
    with tenant_scope(other), pytest.raises(HTTPException) as rejected:
        await Casework(fresh_store, queries).detail(
            created["case_id"],
            Principal(subject="other", tenant_id=other, roles=frozenset({"operator"})),
        )
    assert rejected.value.status_code == 404
