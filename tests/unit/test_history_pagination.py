"""Older authorized recordings remain reachable despite tombstones and other scopes."""

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sio_api.recording_timeline import RecordingTimeline
from sio_api.workbench_store import decode_cursor
from test_cases import MemoryStore

from sio_core.authn import Principal
from sio_core.tenancy import tenant_scope


async def test_timeline_pages_after_lifecycle_and_scope_filters():
    class Store(MemoryStore):
        async def list_analysis_metadata(self, tenant, limit=500):
            return await self.list(tenant, "analysis", limit)

    store = Store()
    for index in range(35):
        video = f"vid_{index:032x}"
        await store.put(
            "tenant",
            "video",
            video,
            {
                "video_id": video,
                "title": str(index),
                "status": "ready",
                "duration_s": 10,
                "zones": [{"zone_id": "allowed" if index < 5 else "other"}],
                "archived_at": "2026-09-30T00:00:00Z" if index > 20 else None,
            },
        )
        await store.put(
            "tenant",
            "recording_clock",
            video,
            {
                "camera_id": "gate",
                "capture_started_at": "2026-09-30T10:00:00+00:00",
                "clock_offset_s": 0,
                "zones": [{"zone_id": "allowed" if index < 5 else "other"}],
            },
        )
    principal = Principal(
        subject="reader",
        tenant_id="tenant",
        roles=frozenset({"viewer"}),
        zones=frozenset({"allowed"}),
    )
    manager = RecordingTimeline(store)
    with tenant_scope("tenant"):
        first = await manager.timeline(principal, limit=2)
        second = await manager.timeline(principal, limit=2, cursor=first["next_cursor"])
        last = await manager.timeline(principal, limit=2, cursor=second["next_cursor"])
        identifiers = [
            row["video_id"] for page in (first, second, last) for row in page["recordings"]
        ]
        assert len(set(identifiers)) == len(identifiers) == 5
        assert last["next_cursor"] is None
        assert (await manager.timeline(principal, camera_id="not-there"))["recordings"] == []
        assert (await manager.timeline(principal, start=datetime(2026, 10, 1, tzinfo=UTC)))[
            "recordings"
        ] == []
        with pytest.raises(HTTPException):
            await manager.timeline(principal, start=datetime(2026, 10, 1))
        for start, end in (
            (datetime(2026, 10, 1), datetime(2026, 10, 2, tzinfo=UTC)),
            (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 2)),
        ):
            with pytest.raises(HTTPException) as error:
                await manager.timeline(principal, start=start, end=end)
            assert error.value.status_code == 422


@pytest.mark.parametrize("cursor", ["bad", "e30", "WzFd", "".join(["a"] * 1025)])
def test_malformed_cursor_is_a_client_error(cursor):
    with pytest.raises(HTTPException) as error:
        decode_cursor(cursor)
    assert error.value.status_code == 422
