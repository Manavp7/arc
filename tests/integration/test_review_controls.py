"""Real PostgreSQL review controls; isolated tenant writes always roll back.

All analyses are authored metadata fixtures. No media is written, no decoder is
invoked, and every queue manager is closed before enqueue/recovery can start a worker.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sio_api.review_bookmarks import (
    KIND,
    BookmarkCreate,
    BookmarkDelete,
    BookmarkPatch,
    ReviewBookmarks,
)
from sio_api.review_models import AnalysisRequest
from sio_api.video_review import VideoReviewManager
from sio_api.workbench_store import WorkbenchStore
from test_workbench import database as database

from sio_core.authn import Principal
from sio_core.config import Settings
from sio_core.tenancy import tenant_scope

pytestmark = pytest.mark.infra


async def seed_recording(store, tenant, *, video_id=None, analysis_id=None):
    """Create valid recording metadata without making any private media files."""
    video_id = video_id or "vid_" + uuid4().hex
    zones = [
        {
            "zone_id": "review-area",
            "name": "Authored review area",
            "points": [[0, 0], [1, 0], [1, 1], [0, 1]],
        }
    ]
    rules = [
        {
            "rule_id": "entry",
            "name": "Authored entry rule",
            "zone_id": "review-area",
            "event_type": "entry",
            "target_class": "motion",
            "threshold_s": 1,
            "cooldown_s": 2,
            "enabled": True,
        }
    ]
    if analysis_id:
        await store.put(
            tenant,
            "analysis",
            analysis_id,
            {
                "analysis_id": analysis_id,
                "video_id": video_id,
                "status": "completed",
                "progress": 1,
                "zones": zones,
                "rules": rules,
                "events": [],
                "detections": [],
                "model": {"mode": "motion", "name": "Authored integration fixture"},
                "source": "recorded_file",
                "privacy": "full_frame_pixelation",
            },
            expected_revision=0,
        )
    return await store.put(
        tenant,
        "video",
        video_id,
        {
            "video_id": video_id,
            "title": "Authored review controls fixture; no media",
            "duration_s": 12,
            "status": "completed" if analysis_id else "ready",
            "zones": zones,
            "rules": rules,
            "analysis_id": analysis_id,
            "source": "recorded_file",
            "privacy": "full_frame_pixelation",
        },
        expected_revision=0,
    )


def closed_manager(settings, store):
    manager = VideoReviewManager(settings, store)
    manager.closing = True
    return manager


async def test_postgres_explicit_motion_profile_survives_recovery_and_retry(database, tmp_path):
    queries, (tenant, other) = database
    settings = Settings(data_dir=tmp_path / "no-worker-media", det_conf=0.35, det_imgsz=640)
    store = WorkbenchStore(queries)
    video = await seed_recording(store, tenant)
    manager = closed_manager(settings, store)
    first = await manager.analyze(
        tenant,
        video["video_id"],
        "integration-reviewer",
        AnalysisRequest(mode="motion", confidence_threshold=0.41, sample_fps=1),
    )
    first_job = await store.get(tenant, "video_job", first["job_id"])
    pinned = first["profile"]
    assert pinned["requested_mode"] == pinned["resolved_mode"] == "motion"
    assert pinned["sample_fps"] == 1 and pinned["confidence_threshold"] == 0.41
    assert pinned["weights_sha256"] is None and pinned["imgsz"] == 640
    assert first_job["configuration"]["profile"] == pinned
    assert manager.task is None

    # Simulate a process stopping during an attempt, without starting the processor.
    await store.put(
        tenant,
        "analysis",
        first["analysis_id"],
        {**first, "status": "running", "progress": 0.5},
        expected_revision=first["revision"],
    )
    await store.put(
        tenant,
        "video_job",
        first_job["job_id"],
        {**first_job, "status": "running"},
        expected_revision=first_job["revision"],
    )
    changed_defaults = settings.model_copy(update={"det_conf": 0.8, "det_imgsz": 320})
    recovered_store = WorkbenchStore(queries)
    restarted = closed_manager(changed_defaults, recovered_store)
    await restarted.ensure_recovered(tenant)
    recovered_job = await recovered_store.get(tenant, "video_job", first_job["job_id"])
    recovered_analysis = await recovered_store.get(tenant, "analysis", recovered_job["analysis_id"])
    interrupted = await recovered_store.get(tenant, "analysis", first["analysis_id"])
    assert recovered_job["status"] == "queued" and recovered_job["attempt"] == 2
    assert recovered_analysis["analysis_id"] != first["analysis_id"]
    assert interrupted["status"] == "interrupted" and interrupted["profile"] == pinned
    assert recovered_job["configuration"] == first_job["configuration"]
    assert recovered_analysis["profile"] == pinned
    assert restarted.task is None

    cancelled = await restarted.cancel(tenant, recovered_job["job_id"], recovered_job["revision"])
    current_video = await recovered_store.get(tenant, "video", video["video_id"])
    await recovered_store.put(
        tenant,
        "video",
        video["video_id"],
        {
            **current_video,
            "rules": [{**current_video["rules"][0], "threshold_s": 9, "cooldown_s": 10}],
        },
        expected_revision=current_video["revision"],
    )
    retry_store = WorkbenchStore(queries)
    retried_manager = closed_manager(changed_defaults, retry_store)
    retry_job = await retried_manager.retry_job(
        tenant, cancelled["job_id"], cancelled["revision"], "integration-reviewer"
    )
    retried_analysis = await retry_store.get(tenant, "analysis", retry_job["analysis_id"])
    assert retry_job["job_id"] != cancelled["job_id"]
    assert retry_job["retry_of"] == first_job["job_id"] and retry_job["status"] == "queued"
    assert retry_job["configuration"] == first_job["configuration"]
    assert retried_analysis["profile"] == pinned
    assert retried_analysis["rules"] == first["rules"]
    assert retried_analysis["analysis_id"] not in recovered_job["analysis_ids"]
    assert (await retry_store.get(tenant, "video", video["video_id"]))["analysis_id"] == (
        retried_analysis["analysis_id"]
    )
    assert await retry_store.get(other, "video_job", retry_job["job_id"]) is None
    history = await retry_store.history(tenant, "analysis", first["analysis_id"])
    assert [entry["payload"]["status"] for entry in history] == [
        "interrupted",
        "running",
        "queued",
    ]
    assert all(entry["payload"]["profile"] == pinned for entry in history)
    assert retried_manager.task is None and not settings.data_dir.exists()


async def test_postgres_personal_bookmarks_pin_retained_analysis_and_enforce_delete_cas(database):
    queries, (tenant, other) = database
    store = WorkbenchStore(queries)
    old_id, newer_id = "ana_" + uuid4().hex, "ana_" + uuid4().hex
    video = await seed_recording(store, tenant, analysis_id=old_id)
    owner = Principal(
        subject="reviewer", tenant_id=tenant, roles=frozenset({"operator"}), clearance=3
    )
    colleague = Principal(
        subject="different-reviewer", tenant_id=tenant, roles=frozenset({"admin"}), clearance=3
    )
    other_owner = Principal(
        subject="reviewer", tenant_id=other, roles=frozenset({"operator"}), clearance=3
    )
    body = BookmarkCreate(
        analysis_id=old_id, at_s=2.5, title="  Check this moment  ", note="Authored fixture note"
    )
    with tenant_scope(tenant):
        bookmarks = ReviewBookmarks(store)
        created = await bookmarks.create(video["video_id"], body, owner)
        updated = await bookmarks.patch(
            created["bookmark_id"],
            BookmarkPatch(expected_revision=created["revision"], at_s=3.25, note="Reviewed cue"),
            owner,
        )
        private_to_colleague = await bookmarks.create(video["video_id"], body, colleague)
    assert created["revision"] == 1 and created["title"] == "Check this moment"
    assert updated["revision"] == 2 and updated["analysis_id"] == old_id

    retained = await store.get(tenant, "analysis", old_id)
    await store.put(
        tenant,
        "analysis",
        newer_id,
        {**retained, "analysis_id": newer_id, "model": {"mode": "motion", "name": "Later fixture"}},
        expected_revision=0,
    )
    current_video = await store.get(tenant, "video", video["video_id"])
    await store.put(
        tenant,
        "video",
        video["video_id"],
        {**current_video, "analysis_id": newer_id},
        expected_revision=current_video["revision"],
    )
    # Same video and analysis IDs in a second tenant must still have separate notes.
    await seed_recording(store, other, video_id=video["video_id"], analysis_id=old_id)
    with tenant_scope(other):
        other_note = await ReviewBookmarks(store).create(video["video_id"], body, other_owner)

    fresh_store = WorkbenchStore(queries)
    fresh = ReviewBookmarks(fresh_store)
    with tenant_scope(tenant):
        assert (await fresh.list(video["video_id"], owner))["bookmarks"] == [updated]
        assert (await fresh.list(video["video_id"], owner, old_id))["bookmarks"] == [updated]
        assert (await fresh.list(video["video_id"], owner, newer_id))["bookmarks"] == []
        assert (await fresh.list(video["video_id"], colleague))["bookmarks"] == [
            private_to_colleague
        ]
        for attempt in (
            fresh.patch(
                updated["bookmark_id"],
                BookmarkPatch(expected_revision=created["revision"], note="Stale edit"),
                owner,
            ),
            fresh.delete(
                updated["bookmark_id"], BookmarkDelete(expected_revision=created["revision"]), owner
            ),
        ):
            with pytest.raises(HTTPException) as rejected:
                await attempt
            assert rejected.value.status_code == 409
        with pytest.raises(HTTPException) as personal:
            await fresh.delete(
                updated["bookmark_id"],
                BookmarkDelete(expected_revision=updated["revision"]),
                colleague,
            )
        assert personal.value.status_code == 404
    with tenant_scope(other):
        assert (await fresh.list(video["video_id"], other_owner))["bookmarks"] == [other_note]
        with pytest.raises(HTTPException) as isolated:
            await fresh.delete(
                updated["bookmark_id"],
                BookmarkDelete(expected_revision=updated["revision"]),
                other_owner,
            )
        assert isolated.value.status_code == 404

    with tenant_scope(tenant):
        assert (await fresh.list(video["video_id"], owner))["bookmarks"] == [updated]
        result = await fresh.delete(
            updated["bookmark_id"], BookmarkDelete(expected_revision=updated["revision"]), owner
        )
        assert result == {"deleted": True, "bookmark_id": updated["bookmark_id"]}
        assert (await ReviewBookmarks(WorkbenchStore(queries)).list(video["video_id"], owner))[
            "bookmarks"
        ] == []
        assert (await fresh.list(video["video_id"], colleague))["bookmarks"] == [
            private_to_colleague
        ]
    assert await fresh_store.get(tenant, KIND, updated["bookmark_id"]) is None
    assert (await fresh_store.get(tenant, "video", video["video_id"]))["analysis_id"] == newer_id
    assert (await fresh_store.get(tenant, "analysis", old_id)) == retained
    assert (await fresh_store.get(other, KIND, other_note["bookmark_id"])) == other_note
