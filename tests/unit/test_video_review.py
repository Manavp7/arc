"""Recorded pixels through upload, processing, rules, persisted evidence and tenant auth."""

from __future__ import annotations

import copy

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sio_api.video_processing import VideoProcessor, media_available
from sio_api.video_review import VideoReviewManager, install_video_routes
from sio_api.video_rules import ReviewConfiguration, ZoneInput, contains, evaluate_rules
from sio_api.workbench_store import WorkbenchConflict

from sio_core.authn import DevJwtAuth
from sio_core.guard import install_governance


class MemoryDocuments:
    def __init__(self):
        self.records = {}

    async def get(self, tenant, kind, key):
        return copy.deepcopy(self.records.get((tenant, kind, key)))

    async def list(self, tenant, kind, limit=500):
        return copy.deepcopy(
            [value for (t, k, _), value in self.records.items() if (t, k) == (tenant, kind)][:limit]
        )

    async def put(self, tenant, kind, key, data, expected_revision=None):
        old = self.records.get((tenant, kind, key))
        revision = old["revision"] if old else 0
        if expected_revision is not None and revision != expected_revision:
            raise WorkbenchConflict("stale revision")
        result = {
            **copy.deepcopy(data),
            "record_id": key,
            "revision": revision + 1,
            "created_at": "2026-09-11T00:00:00Z",
            "updated_at": "2026-09-11T00:00:00Z",
        }
        self.records[tenant, kind, key] = result
        return copy.deepcopy(result)


def configuration(event_type="entry", threshold=1):
    return {
        "zones": [
            {
                "zone_id": "z1",
                "name": "Review area",
                "points": [[0.4, 0.1], [0.95, 0.1], [0.95, 0.95], [0.4, 0.95]],
            }
        ],
        "rules": [
            {
                "rule_id": "r1",
                "name": "Area check",
                "zone_id": "z1",
                "event_type": event_type,
                "target_class": "motion",
                "threshold_s": threshold,
                "cooldown_s": 2,
                "enabled": True,
            }
        ],
    }


@pytest.fixture
def footage(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    path = tmp_path / "authored.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 12, (320, 180))
    assert writer.isOpened()
    for index in range(72):
        image = np.zeros((180, 320, 3), dtype=np.uint8)
        if index >= 12:
            x = min(190, 20 + (index - 12) * 4)
            cv2.rectangle(image, (x, 60), (x + 32, 115), (255, 255, 255), -1)
        writer.write(image)
    writer.release()
    return path.read_bytes()


@pytest.fixture
def workbench(settings):
    settings.audit_enabled = False
    settings.model_dir = settings.data_dir / "absent_models"
    app = FastAPI()
    store = MemoryDocuments()
    manager = install_video_routes(app, settings, store)
    install_governance(app, service="api", settings=settings)
    return app, manager, store


def auth(manager, tenant="tenant-a", role="operator"):
    token = DevJwtAuth(manager.settings).issue(
        subject="reviewer", tenant_id=tenant, roles=(role,), clearance=3
    )
    return {"Authorization": f"Bearer {token}"}


async def test_actual_video_to_persisted_event_and_tenant_scoped_media(workbench, footage):
    if not media_available():
        pytest.skip("Working ffmpeg/ffprobe needed for authored video integration")
    app, manager, store = workbench
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        headers = auth(manager)
        response = await client.post(
            "/api/review/videos",
            content=footage,
            headers={**headers, "Content-Type": "video/mp4", "X-Filename": "authored%20motion.mp4"},
        )
        assert response.status_code == 201, response.text
        video = response.json()
        assert video["title"] == "authored motion.mp4"
        assert video["created_by"] == "reviewer"
        base = f"/api/review/videos/{video['video_id']}"
        response = await client.put(
            base + "/configuration",
            json={"revision": video["revision"], **configuration()},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        revision = response.json()["revision"]
        assert (
            await client.put(
                base + "/configuration",
                json={"revision": video["revision"], **configuration()},
                headers=headers,
            )
        ).status_code == 409
        started = await client.post(base + "/analyze", headers=headers)
        assert started.status_code == 202, started.text
        await manager.task
        response = await client.get(base + "/analysis", headers=headers)
        analysis = response.json()
        assert analysis["status"] == "completed", analysis
        assert analysis["model"]["mode"] == "motion"
        assert len(analysis["detections"]) == 12
        assert analysis["events"]
        event = analysis["events"][0]
        assert event["confidence"] is None
        assert event["analysis_id"] == analysis["analysis_id"]
        assert (await client.get(event["frame_url"], headers=headers)).status_code == 200
        media = await client.get(video["media_url"], headers={**headers, "Range": "bytes=0-31"})
        assert media.status_code == 206
        assert media.headers["cache-control"] == "private, no-store"
        assert media.headers["content-type"] == "video/mp4"
        assert (await client.get(base + "/original.mp4", headers=headers)).status_code == 404
        assert (await client.get(video["media_url"])).status_code == 401
        foreign = auth(manager, tenant="tenant-b")
        for path in (base, base + "/analysis", video["media_url"], event["frame_url"]):
            assert (await client.get(path, headers=foreign)).status_code == 404
        assert (await client.get("/api/review/videos", headers=foreign)).json()["videos"] == []
        before = copy.deepcopy(store.records)
        preview = await client.post(
            base + "/preview", json=configuration("dwell", 180), headers=headers
        )
        assert preview.status_code == 200
        assert preview.json()["events"] == []
        assert store.records == before
        # A new manager can read completed state and its old evidence; reruns create a new version.
        restored = VideoReviewManager(manager.settings, store)
        assert (await restored.video("tenant-a", video["video_id"]))["revision"] > revision
        assert (
            await restored.recover("tenant-a", await restored.video("tenant-a", video["video_id"]))
        )["status"] == "completed"
        repeated = await client.post(base + "/analyze", headers=headers)
        await manager.task
        assert repeated.json()["analysis_id"] != analysis["analysis_id"]
        assert await store.get("tenant-a", "analysis", analysis["analysis_id"]) == analysis
        assert (await client.get(event["frame_url"], headers=headers)).status_code == 200
        assert (await client.get(base + "/analysis", headers=headers)).json()[
            "analysis_id"
        ] == repeated.json()["analysis_id"]
        exact = await client.get(
            base + "/analysis", params={"analysis_id": analysis["analysis_id"]}, headers=headers
        )
        assert exact.status_code == 200
        assert exact.json() == analysis
        assert (
            await client.get(
                base + "/analysis", params={"analysis_id": analysis["analysis_id"]}, headers=foreign
            )
        ).status_code == 404
        for unknown in ("not-an-analysis-id", "ana_" + "f" * 32):
            assert (
                await client.get(
                    base + "/analysis", params={"analysis_id": unknown}, headers=headers
                )
            ).status_code == 404
        other_video = "vid_" + "e" * 32
        await store.put("tenant-a", "video", other_video, {"video_id": other_video})
        wrong_video = await client.get(
            f"/api/review/videos/{other_video}/analysis",
            params={"analysis_id": analysis["analysis_id"]},
            headers=headers,
        )
        assert wrong_video.status_code == 404


async def test_upload_bounds_and_invalid_container_leave_no_private_files(workbench, monkeypatch):
    app, manager, store = workbench
    monkeypatch.setattr(manager, "capabilities", lambda: {"upload": True})
    monkeypatch.setattr("sio_api.video_review.MAX_BYTES", 24)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        headers = {**auth(manager), "Content-Type": "video/mp4"}
        response = await client.post("/api/review/videos", content=b"x" * 25, headers=headers)
        assert response.status_code == 413

        async def chunks():
            yield b"x" * 12
            yield b"x" * 13

        response = await client.post("/api/review/videos", content=chunks(), headers=headers)
        assert response.status_code == 413
        response = await client.post(
            "/api/review/videos", content=b"not an mp4 container", headers=headers
        )
        assert response.status_code == 422
        response = await client.post(
            "/api/review/videos",
            content=b"anything",
            headers={**auth(manager), "Content-Type": "text/plain"},
        )
        assert response.status_code == 415
        response = await client.post(
            "/api/review/videos",
            content=b"anything",
            headers={**auth(manager, role="viewer"), "Content-Type": "video/mp4"},
        )
        assert response.status_code == 403
    assert not list(manager.root.rglob("*.mp4"))
    assert store.records == {}


def test_motion_detections_and_evidence_come_from_actual_pixels(workbench, footage, tmp_path):
    _, manager, _ = workbench
    path = tmp_path / "input.mp4"
    path.write_bytes(footage)
    processor = VideoProcessor(path, manager.settings)
    try:
        assert processor.model["mode"] == "motion"
        assert processor.sample(0, tmp_path / "zero.jpg") == []
        objects = processor.sample(3, tmp_path / "three.jpg")
        assert objects and all(obj["class_name"] == "motion" for obj in objects)
        assert all(obj["confidence"] is None for obj in objects)
        assert (tmp_path / "three.jpg").is_file()
        assert (tmp_path / "three.jpg").read_bytes() != (tmp_path / "zero.jpg").read_bytes()
    finally:
        processor.close()


async def test_failed_conversion_cleans_original_and_does_not_create_record(
    workbench, footage, monkeypatch
):
    app, manager, store = workbench
    monkeypatch.setattr(manager, "capabilities", lambda: {"upload": True})

    def reject(_):
        raise ValueError("Unsupported duration")

    monkeypatch.setattr("sio_api.video_review.probe", reject)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/review/videos",
            content=footage,
            headers={**auth(manager), "Content-Type": "video/mp4"},
        )
    assert response.status_code == 422
    assert store.records == {}
    assert not list(manager.root.rglob("*.mp4"))


async def test_restart_explicitly_marks_incomplete_run_interrupted(workbench):
    _, manager, store = workbench
    vid, aid = "vid_" + "a" * 32, "ana_" + "b" * 32
    video = await store.put(
        "tenant-a", "video", vid, {"video_id": vid, "analysis_id": aid, "status": "analyzing"}
    )
    await store.put(
        "tenant-a",
        "analysis",
        aid,
        {"analysis_id": aid, "video_id": vid, "status": "running", "progress": 0.4},
    )
    await manager.recover("tenant-a", video)
    assert (await store.get("tenant-a", "analysis", aid))["status"] == "interrupted"
    assert (await store.get("tenant-a", "video", vid))["status"] == "interrupted"


async def test_motion_mode_refuses_classification_rule_and_persists_failure(workbench, footage):
    _, manager, store = workbench
    vid = "vid_" + "c" * 32
    directory = manager.directory("tenant-a", vid)
    directory.mkdir(parents=True)
    (directory / "original.mp4").write_bytes(footage)
    config = configuration()
    config["rules"][0]["target_class"] = "person"
    await store.put("tenant-a", "video", vid, {"video_id": vid, "duration_s": 6, **config})
    job = await manager.analyze("tenant-a", vid, "reviewer")
    await manager.task
    result = await store.get("tenant-a", "analysis", job["analysis_id"])
    assert result["status"] == "failed"
    assert result["model"]["mode"] == "motion"
    assert "cannot classify" in result["error"]
    assert result["events"] == []
    assert result["progress"] == 0
    assert not manager.lock.locked()


def test_media_directory_cannot_escape_private_root(workbench, tmp_path):
    _, manager, _ = workbench
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        manager.directory("tenant-a", "../../outside")
    vid = "vid_" + "a" * 32
    target = manager.directory("tenant-a", vid)
    target.parent.mkdir(parents=True)
    target.symlink_to(tmp_path)
    with pytest.raises(HTTPException):
        manager.directory("tenant-a", vid)


@pytest.mark.parametrize(
    "points",
    [
        [[0, 0], [1, 1], [0, 1], [1, 0]],
        [[-1, 0], [1, 0], [1, 1]],
        [[0, 0], [0.1, 0.1], [0.2, 0.2]],
        [[0, 0], [1, 0], [float("nan"), 1]],
    ],
)
def test_polygon_rejects_invalid_geometry(points):
    with pytest.raises(ValidationError):
        ZoneInput(zone_id="z", name="Zone", points=points)


def test_concave_zone_accepts_disjoint_collinear_edges():
    points = [[0, 0], [1, 0], [1, 1], [0.7, 1], [0.7, 0.7], [0.3, 0.7], [0.3, 1], [0, 1]]
    zone = ZoneInput(zone_id="u", name="U shaped area", points=points)
    assert contains((0.5, 0.5), zone.points)
    assert not contains((0.5, 0.9), zone.points)


def test_rule_entry_dwell_cooldown_and_gap_are_deterministic():
    zone = configuration()["zones"][0]["points"]
    assert contains((0.4, 0.5), zone)
    assert not contains((0.1, 0.5), zone)
    analysis = {"analysis_id": "a1", "video_id": "v1", "detections": []}
    for at, x in [
        (0, 0.2),
        (0.5, 0.5),
        (1, 0.5),
        (1.5, 0.5),
        (2, 0.2),
        (2.5, 0.5),
        (4, 0.5),
        (4.5, 0.5),
        (5, 0.5),
    ]:
        analysis["detections"].append(
            {
                "at_s": at,
                "frame_url": f"frame-{at}",
                "objects": [
                    {
                        "track_id": "t1",
                        "class_name": "motion",
                        "confidence": None,
                        "bbox": [x - 0.05, 0.4, x + 0.05, 0.6],
                    }
                ],
            }
        )
    config = ReviewConfiguration.model_validate(configuration("entry"))
    entry = evaluate_rules(analysis, config)
    assert [event["at_s"] for event in entry] == [0.5, 2.5, 4.5]
    assert evaluate_rules(analysis, config) == entry
    dwell = evaluate_rules(analysis, ReviewConfiguration.model_validate(configuration("dwell", 1)))
    assert [event["at_s"] for event in dwell] == [1.5, 5]
