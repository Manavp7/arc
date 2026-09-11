"""Pinned local model choices survive durable queue retries without silent fallbacks."""

import hashlib
import json
import struct
from copy import deepcopy

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sio_api.review_models import AnalysisRequest, inspect_detector, model_catalog, pin_profile
from sio_api.video_processing import VideoProcessor
from sio_api.video_review import VideoReviewManager
from test_video_jobs_storage import queue as queue
from test_video_jobs_storage import seed_video
from test_video_review import auth
from test_video_review import footage as footage


def constant_detector(path, confidence=0.7):
    """Tiny authored ONNX graph using protobuf wire encoding; no download or onnx dependency.

    An unused image input and constant [1,2,6] detections exercise the real runtime/adapter,
    not model accuracy. Metadata is carried in the artifact exactly as for deployed weights.
    """

    def integer(value):
        result = bytearray()
        while value > 127:
            result.append((value & 127) | 128)
            value >>= 7
        return bytes([*result, value])

    def field(number, value):
        if isinstance(value, int):
            return integer(number << 3) + integer(value)
        value = value.encode() if isinstance(value, str) else value
        return integer((number << 3) | 2) + integer(len(value)) + value

    def tensor_type(name, dimensions):
        shape = b"".join(field(1, field(1, dim)) for dim in dimensions)
        return field(1, name) + field(2, field(1, field(1, 1) + field(2, shape)))

    tensor = (
        b"".join(field(1, dim) for dim in [1, 2, 6])
        + field(2, 1)
        + field(8, "output")
        + field(
            9, struct.pack("<12f", 100, 200, 300, 400, confidence, 0, 350, 250, 450, 350, 0.2, 0)
        )
    )
    graph = (
        field(2, "authored_constant_detector")
        + field(5, tensor)
        + field(11, tensor_type("images", [1, 3, 640, 640]))
        + field(12, tensor_type("output", [1, 2, 6]))
    )
    data = (
        field(1, 8)
        + field(2, "SIO authored regression fixture")
        + field(7, graph)
        + field(8, field(2, 13))
        + field(14, field(1, "names") + field(2, '{"0":"fixture-person"}'))
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def installed(queue):
    pytest.importorskip("onnxruntime")
    pytest.importorskip("cv2")
    manager, _, _ = queue
    manager.settings.model_dir = manager.settings.data_dir / "profile-models"
    manager.settings.det_model = "configured.onnx"
    path = manager.settings.model_dir / manager.settings.det_model
    digest = constant_detector(path)
    inspect_detector.cache_clear()
    return path, digest


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "external"},
        {"weights": "/tmp/model.onnx"},
        {"model_id": "arbitrary"},
        {"sample_fps": 0},
        {"sample_fps": 3},
        {"sample_fps": 1.5},
        {"sample_fps": True},
        {"sample_fps": "2"},
        {"confidence_threshold": float("nan")},
        {"confidence_threshold": float("inf")},
        {"confidence_threshold": 0.01},
        {"confidence_threshold": 1},
        {"confidence_threshold": True},
    ],
)
def test_analysis_request_rejects_paths_unknown_models_unbounded_or_nonfinite_controls(body):
    with pytest.raises(ValidationError):
        AnalysisRequest.model_validate(body)


def test_catalog_advertises_only_configured_compatible_detector_with_embedded_labels(
    queue, installed
):
    manager, _, _ = queue
    path, digest = installed
    (path.parent / "unconfigured.onnx").write_bytes(b"not selected")
    catalog = model_catalog(manager.settings)
    assert len(catalog["models"]) == 2
    detector = catalog["models"][1]
    assert detector["available"] is True
    assert detector["model_id"] == "configured_onnx" and detector["weights_sha256"] == digest
    assert detector["classes"] == ["fixture-person"]
    assert str(path.parent) not in json.dumps(catalog)
    assert catalog["sample_fps"] == [1, 2]


def test_real_processor_honors_explicit_motion_and_onnx_confidence(
    queue, installed, footage, tmp_path
):
    manager, _, _ = queue
    path = tmp_path / "clip.mp4"
    path.write_bytes(footage)
    for mode, threshold, count in [("onnx", 0.5, 1), ("onnx", 0.8, 0), ("motion", 0.5, 0)]:
        profile = pin_profile(
            manager.settings,
            AnalysisRequest(mode=mode, confidence_threshold=threshold, sample_fps=1),
        )
        processor = VideoProcessor(path, manager.settings, profile)
        try:
            objects = processor.sample(0, tmp_path / f"{mode}-{threshold}.jpg")
            assert len(objects) == count
            assert processor.model["mode"] == mode and processor.model["sample_fps"] == 1
            assert processor.model["confidence_threshold"] == (
                threshold if mode == "onnx" else None
            )
            if objects:
                assert (
                    objects[0]["class_name"] == "fixture-person" and objects[0]["confidence"] == 0.7
                )
            if mode == "motion":
                assert processor.detector is None and "weights_sha256" not in processor.model
        finally:
            processor.close()


async def test_profile_is_durable_and_worker_samples_requested_rate_without_changing_video_rules(
    queue, installed, footage
):
    manager, store, _ = queue
    manager.closing = True
    video = await seed_video(manager, store)
    directory = manager.directory("tenant-a", video["video_id"])
    (directory / "original.mp4").write_bytes(footage)
    options = AnalysisRequest(mode="onnx", confidence_threshold=0.5, sample_fps=1)
    analysis = await manager.analyze("tenant-a", video["video_id"], "reviewer", options)
    job = await store.get("tenant-a", "video_job", analysis["job_id"])
    assert job["configuration"]["profile"] == analysis["profile"]
    assert analysis["profile"]["weights_sha256"] == installed[1]
    assert (await store.get("tenant-a", "video", video["video_id"]))["rules"] == video["rules"]
    assert "profile" not in await store.get("tenant-a", "video", video["video_id"])
    # Settings drift after enqueue cannot change the requested threshold or frame sampling.
    manager.settings.det_conf = 0.9
    manager.closing = False
    manager.kick()
    await manager.task
    finished = await store.get("tenant-a", "analysis", analysis["analysis_id"])
    assert finished["status"] == "completed", finished
    assert [frame["at_s"] for frame in finished["detections"]] == [0]
    assert finished["detections"][0]["objects"][0]["confidence"] == 0.7
    assert finished["model"]["confidence_threshold"] == 0.5


async def test_http_rejects_unavailable_explicit_onnx_and_conflicting_active_profile(queue):
    manager, store, app = queue
    manager.settings.model_dir = manager.settings.data_dir / "missing"
    manager.closing = True
    video = await seed_video(manager, store)
    path = f"/api/review/videos/{video['video_id']}/analyze"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (
            await client.post(path, headers=auth(manager), json={"mode": "onnx"})
        ).status_code == 422
        assert not await manager.jobs("tenant-a")
        assert (
            await client.post(path, headers=auth(manager, role="viewer"), json={"mode": "motion"})
        ).status_code == 403
        assert (
            await client.post(
                path, headers=auth(manager, tenant="tenant-b"), json={"mode": "motion"}
            )
        ).status_code == 404
        result = await client.post(
            path, headers=auth(manager), json={"mode": "motion", "sample_fps": 1}
        )
        assert result.status_code == 202
        repeated = await client.post(
            path, headers=auth(manager), json={"mode": "motion", "sample_fps": 1}
        )
        assert repeated.json()["analysis_id"] == result.json()["analysis_id"]
        assert (
            await client.post(path, headers=auth(manager), json={"mode": "motion", "sample_fps": 2})
        ).status_code == 409
        assert (
            await client.post(path, headers=auth(manager), json={"sample_fps": 1.5})
        ).status_code == 422


@pytest.mark.parametrize("change", ["replace", "remove"])
async def test_worker_and_retry_reject_changed_or_missing_pinned_artifact(queue, installed, change):
    manager, store, _ = queue
    manager.closing = True
    video = await seed_video(manager, store)
    original = await manager.analyze(
        "tenant-a", video["video_id"], "reviewer", AnalysisRequest(mode="onnx")
    )
    if change == "replace":
        constant_detector(installed[0], confidence=0.9)
    else:
        installed[0].unlink()
    manager.closing = False
    manager.kick()
    await manager.task
    failed = await store.get("tenant-a", "analysis", original["analysis_id"])
    assert failed["status"] == "failed" and failed["detections"] == []
    assert failed["profile"] == original["profile"]
    job = await store.get("tenant-a", "video_job", original["job_id"])
    with pytest.raises(HTTPException) as rejected:
        await manager.retry_job("tenant-a", job["job_id"], job["revision"], "reviewer")
    assert rejected.value.status_code == 409
    assert await store.get("tenant-a", "analysis", original["analysis_id"]) == failed
    assert len(await manager.jobs("tenant-a")) == 1


async def test_cancel_retry_and_restart_reuse_exact_profile_and_keep_previous_analysis(
    queue, installed, footage
):
    manager, store, _ = queue
    manager.closing = True
    video = await seed_video(manager, store)
    (manager.directory("tenant-a", video["video_id"]) / "original.mp4").write_bytes(footage)
    original = await manager.analyze(
        "tenant-a",
        video["video_id"],
        "reviewer",
        AnalysisRequest(mode="motion", confidence_threshold=0.6, sample_fps=1),
    )
    job = await store.get("tenant-a", "video_job", original["job_id"])
    cancelled = await manager.cancel("tenant-a", job["job_id"], job["revision"])
    old = deepcopy(await store.get("tenant-a", "analysis", original["analysis_id"]))
    manager.settings.det_conf = 0.8
    retry = await manager.retry_job(
        "tenant-a", cancelled["job_id"], cancelled["revision"], "reviewer"
    )
    assert retry["configuration"]["profile"] == original["profile"]
    attempt = await store.get("tenant-a", "analysis", retry["analysis_id"])
    await store.put(
        "tenant-a", "analysis", attempt["analysis_id"], {**attempt, "status": "running"}
    )
    await store.put("tenant-a", "video_job", retry["job_id"], {**retry, "status": "running"})
    restored = VideoReviewManager(manager.settings, store)
    await restored.ensure_recovered("tenant-a")
    await restored.task
    recovered_job = await store.get("tenant-a", "video_job", retry["job_id"])
    result = await store.get("tenant-a", "analysis", recovered_job["analysis_id"])
    assert result["status"] == "completed" and result["profile"] == original["profile"]
    assert result["model"]["mode"] == "motion" and len(result["detections"]) == 1
    assert result["analysis_id"] not in (original["analysis_id"], attempt["analysis_id"])
    assert await store.get("tenant-a", "analysis", original["analysis_id"]) == old
    assert (await store.get("tenant-a", "analysis", attempt["analysis_id"]))[
        "status"
    ] == "interrupted"


def test_auto_load_failure_reports_actual_motion_but_explicit_onnx_never_falls_back(
    queue, installed, footage, tmp_path
):
    manager, _, _ = queue
    installed[0].write_bytes(b"invalid ONNX fixture")
    catalog = model_catalog(manager.settings)
    assert catalog["models"][1]["available"] is False
    with pytest.raises(ValueError):
        pin_profile(manager.settings, AnalysisRequest(mode="onnx"))
    profile = pin_profile(manager.settings, AnalysisRequest())
    assert profile["requested_mode"] == "auto" and profile["resolved_mode"] == "motion"
    path = tmp_path / "legacy.mp4"
    path.write_bytes(footage)
    processor = VideoProcessor(path, manager.settings)
    try:
        assert processor.model["mode"] == "motion"
        assert processor.model["requested_mode"] == "auto"
        assert "compatible detector" in processor.model["warning"]
    finally:
        processor.close()


@pytest.mark.parametrize("mode", ["onnx", "auto"])
async def test_pinned_onnx_load_failure_never_silently_changes_actual_mode(
    queue, installed, footage, monkeypatch, mode
):
    manager, store, _ = queue
    manager.closing = True
    video = await seed_video(manager, store)
    (manager.directory("tenant-a", video["video_id"]) / "original.mp4").write_bytes(footage)
    original = await manager.analyze(
        "tenant-a", video["video_id"], "reviewer", AnalysisRequest(mode=mode)
    )
    assert original["profile"]["resolved_mode"] == "onnx"

    def fail_warmup(self):
        raise RuntimeError("Authored runtime load failure")

    monkeypatch.setattr("sio_perception.detectors.onnx_yolo.OnnxYoloDetector.warmup", fail_warmup)
    manager.closing = False
    manager.kick()
    await manager.task
    failed = await store.get("tenant-a", "analysis", original["analysis_id"])
    assert failed["status"] == "failed"
    assert "no motion fallback" in failed["error"]
    assert failed["profile"] == original["profile"]
    assert failed["model"]["mode"] == "pending" and not failed["detections"]


async def test_legacy_queued_job_gains_one_actual_profile_for_future_recovery(queue, footage):
    manager, store, _ = queue
    manager.settings.model_dir = manager.settings.data_dir / "missing"
    manager.closing = True
    video = await seed_video(manager, store)
    (manager.directory("tenant-a", video["video_id"]) / "original.mp4").write_bytes(footage)
    original = await manager.analyze("tenant-a", video["video_id"], "reviewer")
    # Authored pre-profile database representation, as persisted by the older queue.
    store.records["tenant-a", "analysis", original["analysis_id"]].pop("profile")
    store.records["tenant-a", "video_job", original["job_id"]]["configuration"].pop("profile")
    manager.closing = False
    manager.kick()
    await manager.task
    complete = await store.get("tenant-a", "analysis", original["analysis_id"])
    job = await store.get("tenant-a", "video_job", original["job_id"])
    assert complete["status"] == "completed" and len(complete["detections"]) == 2
    assert complete["profile"] == job["configuration"]["profile"]
    assert complete["profile"]["requested_mode"] == "auto"
    assert complete["profile"]["resolved_mode"] == complete["model"]["mode"] == "motion"


async def test_public_legacy_video_probes_playback_fps_instead_of_using_source_fps(queue, footage):
    manager, store, app = queue
    manager.closing = True
    video = await seed_video(manager, store)
    await store.put("tenant-a", "video", video["video_id"], {**video, "fps": 60})
    (manager.directory("tenant-a", video["video_id"]) / "playback.mp4").write_bytes(footage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        result = await client.get(f"/api/review/videos/{video['video_id']}", headers=auth(manager))
    assert result.status_code == 200
    assert result.json()["fps"] == 60 and result.json()["playback_fps"] == 12
    assert "playback_fps" not in await store.get("tenant-a", "video", video["video_id"])
