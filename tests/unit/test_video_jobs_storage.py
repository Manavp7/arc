"""Queue recovery and cleanup invariants using only owned temporary media."""

from __future__ import annotations

import asyncio
import copy
import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sio_api.video_jobs import video_mutation_lock
from sio_api.video_review import VideoReviewManager, install_video_routes
from sio_api.video_storage import ArchiveBody, PurgeBody
from test_video_review import MemoryDocuments, auth, configuration

from sio_core.guard import install_governance


@pytest.fixture
def queue(settings):
    settings.audit_enabled = False
    store = MemoryDocuments()
    app = FastAPI()
    manager = install_video_routes(app, settings, store)
    install_governance(app, service="api", settings=settings)
    return manager, store, app


async def seed_video(manager, store, letter="a", tenant="tenant-a"):
    video_id = "vid_" + letter * 32
    directory = manager.directory(tenant, video_id)
    directory.mkdir(parents=True)
    (directory / "original.mp4").write_bytes(b"private original fixture")
    (directory / "playback.mp4").write_bytes(b"pixelated fixture")
    return await store.put(
        tenant,
        "video",
        video_id,
        {
            "video_id": video_id,
            "title": f"Authored {letter}.mp4",
            "duration_s": 1,
            "status": "ready",
            **configuration(),
        },
        expected_revision=0,
    )


class StubProcessor:
    active = 0
    high_water = 0
    entered = None
    release = None

    def __init__(self, *args):
        self.model = {"mode": "motion", "name": "Authored stub"}
        type(self).active += 1
        type(self).high_water = max(type(self).active, type(self).high_water)

    def sample(self, at, path):
        if self.entered:
            self.entered.set()
            assert self.release.wait(5), "Test did not release its owned sample operation"
        path.write_bytes(b"pixelated frame")
        return []

    def close(self):
        type(self).active -= 1


@pytest.fixture
def processor(monkeypatch):
    StubProcessor.active = 0
    StubProcessor.high_water = 0
    StubProcessor.entered = None
    StubProcessor.release = None
    monkeypatch.setattr("sio_api.video_review.VideoProcessor", StubProcessor)
    return StubProcessor


async def test_multiple_videos_queue_and_duplicate_enqueue_is_idempotent(queue, processor):
    manager, store, _ = queue
    first, second = await seed_video(manager, store, "a"), await seed_video(manager, store, "b")
    # The global processor is occupied, so both requests must still accept a durable queue entry.
    await manager.lock.acquire()
    one = await manager.analyze("tenant-a", first["video_id"], "reviewer")
    two = await manager.analyze("tenant-a", second["video_id"], "reviewer")
    again = await manager.analyze("tenant-a", first["video_id"], "reviewer")
    assert one["analysis_id"] == again["analysis_id"]
    assert one["job_id"] != two["job_id"]
    assert len(await manager.jobs("tenant-a")) == 2
    manager.lock.release()
    await manager.task
    assert processor.high_water == 1
    assert all(job["status"] == "completed" for job in await manager.jobs("tenant-a"))


async def test_queued_cancel_and_retry_preserve_old_run_and_saved_configuration(queue, processor):
    manager, store, _ = queue
    manager.closing = True
    video = await seed_video(manager, store)
    analysis = await manager.analyze("tenant-a", video["video_id"], "reviewer")
    job = await store.get("tenant-a", "video_job", analysis["job_id"])
    cancelled = await manager.cancel("tenant-a", job["job_id"], job["revision"])
    old = await store.get("tenant-a", "analysis", analysis["analysis_id"])
    assert old["status"] == "cancelled" and old["detections"] == []
    current = await manager.video("tenant-a", video["video_id"])
    await store.put(
        "tenant-a", "video", video["video_id"], {**current, **configuration("dwell", 180)}
    )
    retry = await manager.retry_job(
        "tenant-a", cancelled["job_id"], cancelled["revision"], "reviewer"
    )
    assert retry["job_id"] != cancelled["job_id"]
    assert retry["analysis_id"] != analysis["analysis_id"]
    assert retry["configuration"]["rules"][0]["event_type"] == "entry"
    manager.closing = False
    manager.kick()
    await manager.task
    assert (await store.get("tenant-a", "analysis", retry["analysis_id"]))["status"] == "completed"
    assert await store.get("tenant-a", "analysis", analysis["analysis_id"]) == old


async def test_running_cancel_waits_for_sample_and_never_completes_evidence(queue, processor):
    manager, store, _ = queue
    video = await seed_video(manager, store)
    processor.entered, processor.release = threading.Event(), threading.Event()
    analysis = await manager.analyze("tenant-a", video["video_id"], "reviewer")
    assert await asyncio.to_thread(processor.entered.wait, 5)
    job = await store.get("tenant-a", "video_job", analysis["job_id"])
    result = await manager.cancel("tenant-a", job["job_id"], job["revision"])
    assert result["status"] == "cancelling"
    processor.release.set()
    await manager.task
    final = await store.get("tenant-a", "analysis", analysis["analysis_id"])
    assert final["status"] == "cancelled" and final["events"] == []
    assert (await store.get("tenant-a", "video_job", job["job_id"]))["status"] == "cancelled"
    assert processor.active == 0


async def test_restart_creates_new_versions_and_exhausts_three_attempt_budget(queue):
    manager, store, _ = queue
    manager.closing = True
    video = await seed_video(manager, store)
    original = await manager.analyze("tenant-a", video["video_id"], "reviewer")
    seen = []
    for attempt in range(1, 4):
        job = await store.get("tenant-a", "video_job", original["job_id"])
        assert job["attempt"] == attempt
        seen.append(job["analysis_id"])
        analysis = await store.get("tenant-a", "analysis", job["analysis_id"])
        await store.put(
            "tenant-a",
            "analysis",
            job["analysis_id"],
            {**analysis, "status": "running", "progress": 0.25},
        )
        await store.put("tenant-a", "video_job", job["job_id"], {**job, "status": "running"})
        restored = VideoReviewManager(manager.settings, store)
        restored.closing = True
        await restored.ensure_recovered("tenant-a")
        assert (await store.get("tenant-a", "analysis", job["analysis_id"]))[
            "status"
        ] == "interrupted"
    final = await store.get("tenant-a", "video_job", original["job_id"])
    assert final["status"] == "failed" and final["attempt"] == 3
    assert final["analysis_ids"] == seen and len(set(seen)) == 3
    assert "exhausted" in final["error"]


async def test_restart_repairs_video_pointer_after_completed_job_commit(queue):
    manager, store, _ = queue
    manager.closing = True
    video = await seed_video(manager, store)
    analysis = await manager.analyze("tenant-a", video["video_id"], "reviewer")
    job = await store.get("tenant-a", "video_job", analysis["job_id"])
    await store.put(
        "tenant-a", "analysis", analysis["analysis_id"], {**analysis, "status": "completed"}
    )
    await store.put("tenant-a", "video_job", job["job_id"], {**job, "status": "completed"})
    restored = VideoReviewManager(manager.settings, store)
    restored.closing = True
    await restored.ensure_recovered("tenant-a")
    assert (await restored.video("tenant-a", video["video_id"]))["status"] == "completed"


async def test_startup_discovers_and_processes_queued_work_for_all_tenants(
    queue, processor, monkeypatch
):
    manager, store, _ = queue
    manager.closing = True
    for tenant in ("tenant-a", "tenant-b"):
        video = await seed_video(manager, store, tenant=tenant)
        await manager.analyze(tenant, video["video_id"], "reviewer")

    async def tenants(kind):
        assert kind == "video_job"
        return ["tenant-a", "tenant-b"]

    monkeypatch.setattr(store, "list_tenants", tenants, raising=False)
    restarted = VideoReviewManager(manager.settings, store)
    await restarted.start()
    await restarted.task
    assert all(
        [
            (await restarted.jobs(tenant))[0]["status"] == "completed"
            for tenant in ("tenant-a", "tenant-b")
        ]
    )
    assert processor.high_water == 1


async def test_upload_accepts_body_while_analysis_waits_and_serializes_tenant_quota(
    queue, monkeypatch
):
    manager, store, app = queue
    monkeypatch.setattr(manager, "capabilities", lambda: {"upload": True})
    monkeypatch.setattr("sio_api.video_review.MAX_VIDEOS", 1)
    monkeypatch.setattr(
        "sio_api.video_review.probe",
        lambda path: {"duration_s": 1, "width": 32, "height": 32, "fps": 1},
    )
    monkeypatch.setattr(
        "sio_api.video_review.prepare_derivatives",
        lambda directory, info: (directory / "playback.mp4").write_bytes(b"pixelated"),
    )
    received = asyncio.Event()

    async def chunks():
        yield b"\x00\x00\x00\x18ftypisom" + b"owned test fixture"
        received.set()

    await manager.lock.acquire()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        headers = {**auth(manager), "Content-Type": "video/mp4"}
        first = asyncio.create_task(
            client.post("/api/review/videos", content=chunks(), headers=headers)
        )
        await asyncio.wait_for(received.wait(), 2)
        assert not first.done()
        second = asyncio.create_task(
            client.post(
                "/api/review/videos",
                content=b"\x00\x00\x00\x18ftypisomowned second fixture",
                headers=headers,
            )
        )
        manager.lock.release()
        responses = await asyncio.gather(first, second)
        assert sorted(response.status_code for response in responses) == [201, 409]
    assert len(await store.list("tenant-a", "video")) == 1


@pytest.mark.parametrize(
    "kind",
    ["case", "evaluation_draft", "evaluation_annotations", "evaluation_report", "evidence_package"],
)
async def test_archive_protects_all_case_evaluation_and_package_references(queue, kind):
    manager, store, _ = queue
    video = await seed_video(manager, store)
    await store.put("tenant-a", kind, "reference", {"nested": {"video_ids": [video["video_id"]]}})
    rows = await manager.storage.candidates("tenant-a", [video["video_id"]], manual=True)
    assert not rows[0]["eligible"]
    with pytest.raises(HTTPException) as error:
        await manager.storage.archive(
            "tenant-a",
            ArchiveBody(
                videos=[{"video_id": video["video_id"], "revision": video["revision"]}]
            ).videos,
            "admin",
        )
    assert error.value.status_code == 409
    assert manager.directory("tenant-a", video["video_id"]).is_dir()


async def test_archive_revalidates_links_revision_and_active_jobs(queue):
    manager, store, _ = queue
    video = await seed_video(manager, store)
    assert (await manager.storage.candidates("tenant-a", [video["video_id"]], manual=True))[0][
        "eligible"
    ]
    # A link added after preview must prevent archive without modifying files.
    await store.put("tenant-a", "case", "case1", {"source_id": video["video_id"]})
    with pytest.raises(HTTPException):
        await manager.storage.archive(
            "tenant-a",
            ArchiveBody(
                videos=[{"video_id": video["video_id"], "revision": video["revision"]}]
            ).videos,
            "admin",
        )
    other = await seed_video(manager, store, "b")
    changed = await store.put("tenant-a", "video", other["video_id"], {**other, "title": "changed"})
    with pytest.raises(HTTPException):
        await manager.storage.archive(
            "tenant-a",
            ArchiveBody(
                videos=[{"video_id": other["video_id"], "revision": other["revision"]}]
            ).videos,
            "admin",
        )
    manager.closing = True
    await manager.analyze("tenant-a", other["video_id"], "reviewer")
    active = (await manager.storage.candidates("tenant-a", [other["video_id"]], manual=True))[0]
    assert "active_job" in active["reasons"] and active["revision"] > changed["revision"]


async def test_archive_restore_and_confirmed_temporary_media_purge(queue):
    manager, store, app = queue
    video = await seed_video(manager, store)
    directory = manager.directory("tenant-a", video["video_id"])
    original = (directory / "original.mp4").read_bytes()
    selected = ArchiveBody(
        videos=[{"video_id": video["video_id"], "revision": video["revision"]}]
    ).videos
    result = await manager.storage.archive("tenant-a", selected, "admin")
    assert result["reclaimed_bytes"] == 0 and (directory / "original.mp4").read_bytes() == original
    archived = result["videos"][0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        headers = auth(manager, role="admin")
        assert (await client.get("/api/review/videos", headers=headers)).json()["videos"] == []
        restored = await client.post(
            f"/api/review/storage/restore/{video['video_id']}",
            json={"revision": archived["revision"]},
            headers=headers,
        )
        assert restored.status_code == 200, restored.text
        video = restored.json()
        assert not video["archived_at"]
    archived = (
        await manager.storage.archive(
            "tenant-a",
            ArchiveBody(
                videos=[{"video_id": video["video_id"], "revision": video["revision"]}]
            ).videos,
            "admin",
        )
    )["videos"][0]
    preview = await manager.storage.purge_preview("tenant-a", [video["video_id"]], "admin")
    assert preview["reclaimable_bytes"] == len(original) + len(b"pixelated fixture")
    body = PurgeBody(
        preview_token=preview["preview_token"], confirmed_video_ids=[video["video_id"]]
    )
    purged = await manager.storage.purge("tenant-a", body, "admin")
    assert purged["reclaimed_bytes"] == preview["reclaimable_bytes"]
    assert not directory.exists()
    tombstone = await store.get("tenant-a", "video", video["video_id"])
    assert tombstone["purged_at"] and tombstone["revision"] > archived["revision"]
    with pytest.raises(HTTPException):
        await manager.storage.purge("tenant-a", body, "admin")
    with pytest.raises(HTTPException):
        await manager.video("tenant-a", video["video_id"])


async def test_purge_confirmation_expiry_actor_and_new_reference_guards(queue):
    manager, store, _ = queue
    video = await seed_video(manager, store)
    await manager.storage.archive(
        "tenant-a",
        ArchiveBody(videos=[{"video_id": video["video_id"], "revision": video["revision"]}]).videos,
        "admin",
    )
    preview = await manager.storage.purge_preview("tenant-a", [video["video_id"]], "admin")
    body = PurgeBody(
        preview_token=preview["preview_token"], confirmed_video_ids=[video["video_id"]]
    )
    for tenant, actor in [("tenant-b", "admin"), ("tenant-a", "someone else")]:
        with pytest.raises(HTTPException) as error:
            await manager.storage.purge(tenant, body, actor)
        assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        await manager.storage.purge(
            "tenant-a",
            PurgeBody(preview_token=body.preview_token, confirmed_video_ids=["vid_" + "f" * 32]),
            "admin",
        )
    assert error.value.status_code == 422
    record = await store.get("tenant-a", "storage_preview", body.preview_token)
    await store.put(
        "tenant-a",
        "storage_preview",
        body.preview_token,
        {**record, "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()},
    )
    with pytest.raises(HTTPException) as error:
        await manager.storage.purge("tenant-a", body, "admin")
    assert error.value.status_code == 409
    preview = await manager.storage.purge_preview("tenant-a", [video["video_id"]], "admin")
    await store.put("tenant-a", "evaluation_report", "report", {"video_ids": [video["video_id"]]})
    with pytest.raises(HTTPException):
        await manager.storage.purge(
            "tenant-a",
            PurgeBody(
                preview_token=preview["preview_token"], confirmed_video_ids=[video["video_id"]]
            ),
            "admin",
        )
    assert (manager.directory("tenant-a", video["video_id"]) / "original.mp4").exists()


async def test_reference_scan_limit_fails_closed_and_archive_serializes_reference_writes(
    queue, monkeypatch
):
    manager, store, _ = queue
    video = await seed_video(manager, store)
    original_list = store.list

    async def bounded(tenant, kind, limit=500):
        if kind == "case":
            return [{"source_id": "unrelated"}] * 5000
        return await original_list(tenant, kind, limit)

    monkeypatch.setattr(store, "list", bounded)
    assert (
        "reference_scan_incomplete"
        in (await manager.storage.candidates("tenant-a", [video["video_id"]], manual=True))[0][
            "reasons"
        ]
    )
    monkeypatch.setattr(store, "list", original_list)
    async with video_mutation_lock(store, "tenant-a", video["video_id"]):
        archive = asyncio.create_task(
            manager.storage.archive(
                "tenant-a",
                ArchiveBody(
                    videos=[{"video_id": video["video_id"], "revision": video["revision"]}]
                ).videos,
                "admin",
            )
        )
        await asyncio.sleep(0)
        assert not archive.done()
        await store.put("tenant-a", "case", "new", {"source_id": video["video_id"]})
    with pytest.raises(HTTPException):
        await archive


async def test_purge_file_failure_retains_private_tombstone_and_reports_failure(queue, monkeypatch):
    manager, store, _ = queue
    video = await seed_video(manager, store)
    await manager.storage.archive(
        "tenant-a",
        ArchiveBody(videos=[{"video_id": video["video_id"], "revision": video["revision"]}]).videos,
        "admin",
    )
    preview = await manager.storage.purge_preview("tenant-a", [video["video_id"]], "admin")
    monkeypatch.setattr(
        "sio_api.video_storage.shutil.rmtree",
        lambda path: (_ for _ in ()).throw(OSError("authored failure")),
    )
    result = await manager.storage.purge(
        "tenant-a",
        PurgeBody(preview_token=preview["preview_token"], confirmed_video_ids=[video["video_id"]]),
        "admin",
    )
    assert result["videos"][0]["status"] == "purge_failed" and result["reclaimed_bytes"] == 0
    assert (await store.get("tenant-a", "video", video["video_id"]))["status"] == "purge_failed"
    assert (manager.directory("tenant-a", video["video_id"]) / "original.mp4").exists()


async def test_storage_and_job_reads_are_tenant_scoped_and_writes_reject_viewer(queue):
    manager, store, app = queue
    manager.closing = True
    video = await seed_video(manager, store)
    run = await manager.analyze("tenant-a", video["video_id"], "reviewer")
    before = copy.deepcopy(store.records)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (
            await client.get("/api/review/jobs", headers=auth(manager, tenant="tenant-b"))
        ).json()["jobs"] == []
        assert (
            await client.get(
                "/api/review/storage", headers=auth(manager, tenant="tenant-b", role="admin")
            )
        ).json()["videos"] == []
        cancelled = await client.post(
            f"/api/review/jobs/{run['job_id']}/cancel",
            json={"revision": 1},
            headers=auth(manager, role="viewer"),
        )
        assert cancelled.status_code == 403
        settings = await client.put(
            "/api/review/storage/settings",
            json={"revision": 0, "archive_after_days": 7},
            headers=auth(manager, role="viewer"),
        )
        assert settings.status_code == 403
    assert before == store.records


@pytest.mark.parametrize("first_operation", ["analyze", "archive"])
async def test_same_video_http_archive_and_enqueue_are_serialized(queue, first_operation):
    manager, store, app = queue
    manager.closing = True
    video = await seed_video(manager, store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:

        async def analyze():
            return await client.post(
                f"/api/review/videos/{video['video_id']}/analyze", headers=auth(manager)
            )

        async def archive():
            return await client.post(
                "/api/review/storage/archive",
                json={"videos": [{"video_id": video["video_id"], "revision": video["revision"]}]},
                headers=auth(manager, role="admin"),
            )

        operations = {"analyze": analyze, "archive": archive}
        order = [first_operation, "archive" if first_operation == "analyze" else "analyze"]
        async with video_mutation_lock(store, "tenant-a", video["video_id"]):
            tasks = [asyncio.create_task(operations[name]()) for name in order]
            await asyncio.sleep(0)
            assert not any(task.done() for task in tasks)
        results = dict(zip(order, await asyncio.gather(*tasks), strict=True))
    assert (results["analyze"].status_code, results["archive"].status_code) in {
        (202, 409),
        (409, 200),
    }
    record = await store.get("tenant-a", "video", video["video_id"])
    active = [job for job in await manager.jobs("tenant-a") if job["status"] == "queued"]
    assert bool(record.get("archived_at")) != bool(active)
    assert (manager.directory("tenant-a", video["video_id"]) / "original.mp4").exists()


async def test_storage_prefix_permissions_cover_all_cleanup_actions(queue):
    manager, store, app = queue
    video_id = "vid_" + "f" * 32
    requests = [
        ("PUT", "/api/review/storage/settings", {"revision": 0, "archive_after_days": 7}),
        ("POST", "/api/review/storage/preview", {}),
        (
            "POST",
            "/api/review/storage/archive",
            {"videos": [{"video_id": video_id, "revision": 1}]},
        ),
        ("POST", f"/api/review/storage/restore/{video_id}", {"revision": 1}),
        ("POST", "/api/review/storage/purge-preview", {"video_ids": [video_id]}),
        (
            "POST",
            "/api/review/storage/purge",
            {"preview_token": "absent", "confirmed_video_ids": [video_id]},
        ),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (
            await client.get("/api/review/storage", headers=auth(manager, role="integrator"))
        ).status_code == 200
        assert (
            await client.get("/api/review/storage", headers=auth(manager, role="operator"))
        ).status_code == 403
        for method, path, body in requests:
            result = await client.request(
                method, path, json=body, headers=auth(manager, role="integrator")
            )
            assert result.status_code == 403, (path, result.text)
        for action in ("cancel", "retry"):
            path = f"/api/review/jobs/absent/{action}"
            assert (
                await client.post(
                    path, json={"revision": 1}, headers=auth(manager, role="operator")
                )
            ).status_code == 404
            assert (
                await client.post(path, json={"revision": 1}, headers=auth(manager, role="viewer"))
            ).status_code == 403
    assert store.records == {}
