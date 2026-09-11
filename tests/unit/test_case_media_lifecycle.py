"""Active footage discovery and new case references obey the cleanup lifecycle."""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from fastapi import HTTPException
from sio_api.case_helpers import SearchQuery
from sio_api.video_jobs import video_mutation_lock
from test_cases import casework as casework
from test_cases import principal as principal
from test_cases import source_request

from sio_core.tenancy import tenant_scope


@pytest.mark.parametrize(
    "state",
    [
        {"archived_at": "2026-09-11T00:00:00Z"},
        {"purge_started_at": "2026-09-11T00:00:00Z"},
        {"purged_at": "2026-09-11T00:00:00Z"},
        {"status": "purging"},
        {"status": "purge_failed"},
        {"status": "purged"},
        {"status": "archived"},
        {"status": "unavailable"},
        {"media_available": False},
    ],
)
async def test_unavailable_media_cannot_gain_new_case_reference(casework, principal, state):
    video = await casework.store.get("tenant-a", "video", "vid_a")
    await casework.store.put("tenant-a", "video", "vid_a", {**video, **state})
    before = deepcopy(casework.store.records)
    with tenant_scope("tenant-a"), pytest.raises(HTTPException) as error:
        await casework.create(source_request(), principal)
    assert error.value.status_code == 409
    assert casework.store.records == before


async def test_missing_or_mismatched_video_fails_closed_before_creating_case(casework, principal):
    video = await casework.store.get("tenant-a", "video", "vid_a")
    for record in ({**video, "video_id": "different-video"}, None):
        if record:
            await casework.store.put("tenant-a", "video", "vid_a", record)
        else:
            del casework.store.records["tenant-a", "video", "vid_a"]
        with tenant_scope("tenant-a"), pytest.raises(HTTPException) as error:
            await casework.create(source_request(), principal)
        assert error.value.status_code == 404
        assert await casework.store.list("tenant-a", "case") == []


@pytest.mark.parametrize("kind", [None, "video", "video_event"])
async def test_search_hides_archived_and_purged_footage_and_events_but_keeps_cases(
    casework, principal, kind
):
    with tenant_scope("tenant-a"):
        created = await casework.create(source_request(), principal)
        visible = await casework.search(SearchQuery(kind=kind), principal)
        assert any(row["kind"] in {"video", "video_event"} for row in visible["results"])
        video = await casework.store.get("tenant-a", "video", "vid_a")
        for state in (
            {"archived_at": "2026-09-11T00:00:00Z"},
            {"purge_started_at": "2026-09-11T00:00:00Z"},
            {"status": "purged"},
        ):
            await casework.store.put("tenant-a", "video", "vid_a", {**video, **state})
            hidden = await casework.search(SearchQuery(kind=kind), principal)
            assert not any(row["kind"] in {"video", "video_event"} for row in hidden["results"])
            # Existing evidence snapshots remain readable, even if an out-of-band state is corrupt.
            assert (await casework.load(created["case_id"], principal))["evidence"] == created[
                "evidence"
            ]
            case_results = await casework.search(SearchQuery(kind="case"), principal)
            assert case_results["results"][0]["id"] == created["case_id"]


async def test_search_discards_orphan_or_foreign_parent_video_events(casework, principal):
    video = await casework.store.get("tenant-a", "video", "vid_a")
    del casework.store.records["tenant-a", "video", "vid_a"]
    await casework.store.put("tenant-b", "video", "vid_a", video)
    with tenant_scope("tenant-a"):
        results = await casework.search(SearchQuery(kind="video_event"), principal)
    assert results["results"] == []


async def test_case_creation_waits_for_cleanup_lock_then_rechecks_media(casework, principal):
    with tenant_scope("tenant-a"):
        async with video_mutation_lock(casework.store, "tenant-a", "vid_a"):
            pending = asyncio.create_task(casework.create(source_request(), principal))
            await asyncio.sleep(0)
            assert not pending.done()
            video = await casework.store.get("tenant-a", "video", "vid_a")
            await casework.store.put("tenant-a", "video", "vid_a", {**video, "status": "purging"})
        with pytest.raises(HTTPException) as error:
            await pending
    assert error.value.status_code == 409
    assert await casework.store.list("tenant-a", "case") == []
