"""Real PostgreSQL queue/cleanup journal checks; tenant writes roll back, media is temporary."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sio_api.video_review import VideoReviewManager
from sio_api.video_storage import ArchiveBody, PurgeBody, VideoStorage
from sio_api.workbench_store import WorkbenchStore
from test_workbench import database as database

from sio_core.config import Settings

pytestmark = pytest.mark.infra


async def seed(store, tenant):
    video_id = "vid_" + uuid4().hex
    return await store.put(
        tenant,
        "video",
        video_id,
        {
            "video_id": video_id,
            "status": "ready",
            "title": "Authored queue integration fixture",
            "duration_s": 1,
            "zones": [{"zone_id": "z", "name": "Area", "points": [[0, 0], [1, 0], [1, 1], [0, 1]]}],
            "rules": [
                {
                    "rule_id": "r",
                    "name": "Entry",
                    "zone_id": "z",
                    "event_type": "entry",
                    "target_class": "motion",
                    "enabled": True,
                }
            ],
        },
        expected_revision=0,
    )


async def test_postgres_recovery_keeps_interrupted_attempt_and_queues_new_snapshot(
    database, tmp_path
):
    queries, (tenant, other) = database
    settings = Settings(data_dir=tmp_path / "authored-media")
    store = WorkbenchStore(queries)
    manager = VideoReviewManager(settings, store)
    manager.closing = True
    video = await seed(store, tenant)
    first = await manager.analyze(tenant, video["video_id"], "integration-reviewer")
    job = await store.get(tenant, "video_job", first["job_id"])
    await store.put(
        tenant, "analysis", first["analysis_id"], {**first, "status": "running", "progress": 0.5}
    )
    await store.put(tenant, "video_job", job["job_id"], {**job, "status": "running"})
    fresh_store = WorkbenchStore(queries)
    restarted = VideoReviewManager(settings, fresh_store)
    restarted.closing = True
    await restarted.ensure_recovered(tenant)
    recovered = await fresh_store.get(tenant, "video_job", job["job_id"])
    assert recovered["attempt"] == 2 and recovered["status"] == "queued"
    assert recovered["analysis_id"] != first["analysis_id"]
    assert (await fresh_store.get(tenant, "analysis", first["analysis_id"]))[
        "status"
    ] == "interrupted"
    assert (await fresh_store.get(tenant, "analysis", recovered["analysis_id"]))["zones"] == first[
        "zones"
    ]
    assert (await fresh_store.get(tenant, "video", video["video_id"]))["analysis_id"] == recovered[
        "analysis_id"
    ]
    assert tenant in await fresh_store.list_tenants("video_job")
    assert await fresh_store.get(other, "video_job", job["job_id"]) is None
    history = await fresh_store.history(tenant, "analysis", first["analysis_id"])
    assert [entry["payload"]["status"] for entry in history] == ["interrupted", "running", "queued"]


async def test_postgres_confirmed_temp_media_purge_preserves_tombstone_and_revision_history(
    database, tmp_path
):
    queries, (tenant, _) = database
    store = WorkbenchStore(queries)
    manager = VideoReviewManager(Settings(data_dir=tmp_path / "authored-media"), store)
    storage = VideoStorage(manager)
    video = await seed(store, tenant)
    directory = manager.directory(tenant, video["video_id"])
    directory.mkdir(parents=True)
    (directory / "original.mp4").write_bytes(b"authored temporary media, not user footage")
    archive = await storage.archive(
        tenant,
        ArchiveBody(videos=[{"video_id": video["video_id"], "revision": video["revision"]}]).videos,
        "integration-admin",
    )
    assert archive["reclaimed_bytes"] == 0 and directory.exists()
    preview = await storage.purge_preview(tenant, [video["video_id"]], "integration-admin")
    result = await storage.purge(
        tenant,
        PurgeBody(preview_token=preview["preview_token"], confirmed_video_ids=[video["video_id"]]),
        "integration-admin",
    )
    assert result["reclaimed_bytes"] > 0 and not directory.exists()
    fresh = WorkbenchStore(queries)
    tombstone = await fresh.get(tenant, "video", video["video_id"])
    assert tombstone["purged_at"] and tombstone["status"] == "purged"
    history = await fresh.history(tenant, "video", video["video_id"])
    assert [entry["payload"]["status"] for entry in history] == [
        "purged",
        "purging",
        "ready",
        "ready",
    ]
    assert (await fresh.get(tenant, "storage_preview", preview["preview_token"]))["used"] is True
