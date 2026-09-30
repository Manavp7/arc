"""Worker liveness loss must not steal media work that still holds its claim."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sio_api.media_coordination import claim
from sio_api.recorded_search import KIND, IndexRequest, RecordedSearch
from test_cases import MemoryStore

from sio_core.authn import Principal
from sio_core.tenancy import tenant_scope

TENANT = "claimed-search"
VIDEO = "vid_" + "a" * 32
ANALYSIS = "ana_" + "b" * 32
OPERATOR = Principal(
    subject="reviewer", tenant_id=TENANT, roles=frozenset({"operator"}), clearance=3
)


@pytest.mark.parametrize("status", ["queued", "running"])
async def test_lost_presence_cannot_replace_index_with_live_job_claim(monkeypatch, status):
    store = MemoryStore()
    await store.put(
        TENANT,
        KIND,
        VIDEO,
        {
            "video_id": VIDEO,
            "analysis_id": ANALYSIS,
            "status": status,
            "worker_id": "old-worker-with-disconnected-presence",
        },
    )
    original = deepcopy(store.records)
    manager = RecordedSearch(SimpleNamespace(tenant_id=TENANT), store, SimpleNamespace())
    alive = AsyncMock(return_value=False)
    put = AsyncMock(wraps=store.put)
    monkeypatch.setattr(manager.presence, "alive", alive)
    monkeypatch.setattr(store, "put", put)
    body = IndexRequest(video_id=VIDEO, analysis_id=ANALYSIS, consent_private_original=True)
    try:
        # This is the old task's real shared claim, held while waiting for the decoder.
        # Its separate presence session can be gone even though the task is still alive.
        async with claim(store, "recorded-search-job", TENANT, VIDEO) as acquired:
            assert acquired
            with tenant_scope(TENANT), pytest.raises(HTTPException) as rejected:
                await manager.enqueue(body, OPERATOR)
            assert rejected.value.status_code == 409
            assert "previous index attempt" in rejected.value.detail
            alive.assert_awaited_once_with("old-worker-with-disconnected-presence")
            put.assert_not_awaited()
            assert store.records == original
            assert manager.task is None
    finally:
        await manager.close()
