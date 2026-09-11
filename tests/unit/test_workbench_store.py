"""The document boundary binds every operation to a tenant and treats revisions as server data."""

import json
from datetime import UTC, datetime

import pytest
from sio_api.workbench_store import WorkbenchConflict, WorkbenchStore


class RecordingPool:
    def __init__(self, row=None, rows=None, count=1):
        self.row = row
        self.rows = rows or []
        self.count = count
        self.calls = []

    async def fetchrow(self, sql, params):
        self.calls.append((sql, params))
        return self.row

    async def fetch(self, sql, params):
        self.calls.append((sql, params))
        return self.rows

    async def execute(self, sql, params):
        self.calls.append((sql, params))
        return self.count


def stored_row():
    return {
        "tenant_id": "tenant-a",
        "kind": "case",
        "record_id": "case-a",
        "revision": 4,
        "created_at": datetime(2026, 9, 11, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 12, tzinfo=UTC),
        "payload": {
            "title": "Current",
            "record_id": "spoof",
            "revision": 999,
            "created_at": "spoof",
            "updated_at": "spoof",
        },
    }


@pytest.mark.asyncio
async def test_document_get_is_tenant_bound_and_server_metadata_overrides_payload():
    pool = RecordingPool(row=stored_row())
    result = await WorkbenchStore(pool).get("tenant-a", "case", "case-a")
    assert result["record_id"] == "case-a"
    assert result["revision"] == 4
    assert result["created_at"] == "2026-09-11T00:00:00+00:00"
    assert result["updated_at"] == "2026-09-12T00:00:00+00:00"
    sql, params = pool.calls[0]
    assert "tenant_id = %s AND kind = %s AND record_id = %s" in sql
    assert params == ("tenant-a", "case", "case-a")


@pytest.mark.asyncio
async def test_put_strips_forged_revision_metadata_and_compares_revision_atomically():
    pool = RecordingPool(row=stored_row())
    result = await WorkbenchStore(pool).put(
        "tenant-a",
        "case",
        "case-a",
        {
            "title": "Changed",
            "revision": 500,
            "record_id": "other",
            "created_at": "yesterday",
            "updated_at": "tomorrow",
        },
        expected_revision=3,
    )
    sql, params = pool.calls[0]
    assert "UPDATE workbench_documents" in sql
    assert "revision = revision + 1" in sql
    assert "tenant_id = %s AND kind = %s AND record_id = %s AND revision = %s" in sql
    assert params[1:] == ("tenant-a", "case", "case-a", 3)
    assert json.loads(params[0]) == {"title": "Changed"}
    assert result["revision"] == 4


@pytest.mark.asyncio
async def test_stale_or_missing_update_is_conflict_never_an_upsert():
    pool = RecordingPool(row=None)
    with pytest.raises(WorkbenchConflict):
        await WorkbenchStore(pool).put(
            "tenant-a", "case", "missing", {"title": "Changed"}, expected_revision=10
        )
    sql, _ = pool.calls[-1]
    assert "INSERT" not in sql


@pytest.mark.asyncio
async def test_create_only_rejects_existing_identifier():
    pool = RecordingPool(row=None)
    with pytest.raises(WorkbenchConflict):
        await WorkbenchStore(pool).put(
            "tenant-a", "video", "video-a", {"title": "A"}, expected_revision=0
        )
    assert all("DO UPDATE" not in sql for sql, _ in pool.calls)


@pytest.mark.asyncio
async def test_nan_and_negative_revision_fail_before_writing():
    pool = RecordingPool(row=stored_row())
    store = WorkbenchStore(pool)
    with pytest.raises(ValueError):
        await store.put(
            "tenant-a", "video", "video-a", {"duration_s": float("nan")}, expected_revision=0
        )
    assert pool.calls == []
    with pytest.raises(WorkbenchConflict):
        await store.put("tenant-a", "case", "case-a", {}, expected_revision=-1)
    assert pool.calls == []


@pytest.mark.asyncio
async def test_listing_is_bounded_and_returns_server_revisions():
    pool = RecordingPool(rows=[stored_row()])
    store = WorkbenchStore(pool)
    result = await store.list("tenant-a", "case", limit=1_000_000)
    assert result[0]["revision"] == 4
    sql, params = pool.calls[0]
    assert "tenant_id = %s AND kind = %s" in sql
    assert params == ("tenant-a", "case", 5000)
    await store.list("tenant-b", "site", limit=0)
    assert pool.calls[-1][1] == ("tenant-b", "site", 1)


@pytest.mark.asyncio
async def test_delete_revision_mismatch_is_conflict_and_history_read_is_tenant_scoped():
    pool = RecordingPool(count=0)
    store = WorkbenchStore(pool)
    with pytest.raises(WorkbenchConflict):
        await store.delete("tenant-a", "video", "video-a", expected_revision=2)
    sql, params = pool.calls[0]
    assert "tenant_id = %s AND kind = %s AND record_id = %s AND revision = %s" in sql
    assert params == ("tenant-a", "video", "video-a", 2)
    assert await store.delete("tenant-a", "video", "missing") is False
    await store.history("tenant-b", "site", "site-a")
    sql, params = pool.calls[-1]
    assert "tenant_id = %s AND kind = %s AND record_id = %s" in sql
    assert params == ("tenant-b", "site", "site-a")


@pytest.mark.asyncio
async def test_internal_worker_tenant_discovery_is_kind_bound_and_read_only():
    pool = RecordingPool(rows=[{"tenant_id": "tenant-a"}, {"tenant_id": "tenant-b"}])
    store = WorkbenchStore(pool)
    assert await store.list_tenants("video_job") == ["tenant-a", "tenant-b"]
    sql, params = pool.calls[0]
    assert "SELECT DISTINCT tenant_id" in sql and "WHERE kind = %s" in sql
    assert "ORDER BY tenant_id" in sql and params == ("video_job",)
    assert not any(word in sql for word in ("UPDATE", "DELETE", "INSERT"))
    pool.rows = []
    assert await store.list_tenants("evidence_package") == []
    assert pool.calls[-1][1] == ("evidence_package",)
