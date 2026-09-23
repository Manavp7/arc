"""Recorded retrieval preserves private source, exact versions and tenant/zone boundaries."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sio_api import recorded_search as module
from sio_api.recorded_search import (
    INDEX_NAME,
    KIND,
    IndexRequest,
    RecordedSearch,
    SampleReference,
    SearchRequest,
    samples_for,
)
from sio_api.recorded_search_model import MODEL_ID, check_model, vector
from sio_api.video_jobs import video_mutation_lock, video_processor_lock
from test_cases import MemoryStore

from sio_core.authn import Principal
from sio_core.tenancy import tenant_scope

TENANT = "search-tests"
VIDEO = "vid_" + "a" * 32
ANALYSIS = "ana_" + "b" * 32
MODEL_DIRECTORY = Path(__file__).resolve().parents[2] / ".sio" / "models"
OPERATOR = Principal(
    subject="reviewer", tenant_id=TENANT, roles=frozenset({"operator"}), clearance=3
)


def axis(index):
    return [1.0 if i == index else 0.0 for i in range(512)]


class AuthoredModel:
    """Deterministic fixture only; semantic quality is checked separately with real CLIP."""

    def embed_image(self, frame):
        return axis(int(frame) % 2)

    def embed_text(self, text):
        return axis(0 if text == "first" else 1)

    def close(self):
        pass


class AuthoredReader:
    def __init__(self, path):
        self.path = path

    def sample(self, at_s):
        return at_s / 2

    def close(self):
        pass


@pytest.fixture
async def work(tmp_path, monkeypatch):
    store = MemoryStore()
    directory = tmp_path / TENANT / VIDEO
    directory.mkdir(parents=True)
    (directory / "original.mp4").write_bytes(b"authored original; never served")
    (directory / ANALYSIS).mkdir()
    for index in range(8):
        (directory / ANALYSIS / f"{index}.jpg").write_bytes(b"pixelated fixture")
    zone = {"zone_id": "gate", "name": "Gate"}
    await store.put(
        TENANT,
        "video",
        VIDEO,
        {
            "video_id": VIDEO,
            "duration_s": 4,
            "title": "Authored test clip",
            "status": "completed",
            "zones": [zone],
        },
    )
    await store.put(
        TENANT,
        "analysis",
        ANALYSIS,
        {
            "analysis_id": ANALYSIS,
            "video_id": VIDEO,
            "status": "completed",
            "zones": [zone],
            "detections": [{"frame_index": i, "at_s": i / 2, "objects": []} for i in range(8)],
            "events": [
                {
                    "event_id": "ve_real",
                    "event_type": "dwell",
                    "started_at_s": 0,
                    "at_s": 1,
                    "end_s": 1,
                }
            ],
        },
    )
    settings = SimpleNamespace(tenant_id=TENANT)
    manager = RecordedSearch(
        settings,
        store,
        SimpleNamespace(directory=lambda tenant, video_id: tmp_path / tenant / video_id),
    )
    monkeypatch.setattr(
        module, "check_model", lambda _: {"available": True, "model_id": MODEL_ID, "reason": None}
    )
    monkeypatch.setattr(module, "load_model", lambda _: AuthoredModel())
    monkeypatch.setattr(module, "FrameReader", AuthoredReader)
    return manager, directory


def request():
    return IndexRequest(video_id=VIDEO, analysis_id=ANALYSIS, consent_private_original=True)


async def indexed(work):
    manager, _ = work
    result = await manager.enqueue(request(), OPERATOR)
    assert result["status"] == "queued"
    await manager.task
    return await manager.store.get(TENANT, KIND, VIDEO)


async def test_bounded_index_has_no_vectors_in_append_only_metadata_and_real_exact_links(work):
    manager, directory = work
    before = deepcopy(manager.store.records)
    with tenant_scope(TENANT):
        row = await indexed(work)
        assert row["status"] == "ready" and row["sample_count"] == 2
        assert "samples" not in row and "vector" not in json.dumps(row)
        assert (directory / INDEX_NAME).stat().st_mode & 0o777 == 0o600
        payload = json.loads((directory / INDEX_NAME).read_text())
        assert [sample["at_s"] for sample in payload["samples"]] == [0, 2]
        first = await manager.query(SearchRequest(text="first"), OPERATOR)
        assert [row["at_s"] for row in first["results"]] == [0, 2]
        assert first["results"][0]["similarity"] == 1
        assert first["results"][0]["events"] == [{"event_id": "ve_real", "event_type": "dwell"}]
        assert first["results"][1]["events"] == []
        assert first["results"][0]["frame_url"] == f"/api/review/videos/{VIDEO}/frames/{ANALYSIS}/0"
        assert "vector" not in json.dumps(first) and "original" not in json.dumps(first)
        image = await manager.query(
            SearchRequest(
                sample=SampleReference(video_id=VIDEO, analysis_id=ANALYSIS, frame_index=0)
            ),
            OPERATOR,
        )
        assert [row["frame_index"] for row in image["results"]] == [4]
        assert all(manager.store.records[key] == value for key, value in before.items())


@pytest.mark.parametrize("consent", [False, 1, "true", None])
def test_consent_cannot_be_coerced_or_omitted(consent):
    with pytest.raises(ValidationError):
        IndexRequest(video_id=VIDEO, analysis_id=ANALYSIS, consent_private_original=consent)


def test_queries_and_vectors_reject_ambiguous_nonfinite_and_unbounded_input():
    for body in (
        {},
        {"text": " "},
        {"text": "first", "sample": {"video_id": VIDEO, "analysis_id": ANALYSIS, "frame_index": 0}},
        {"text": "x", "limit": 51},
    ):
        with pytest.raises(ValidationError):
            SearchRequest(**body)
    for value in ([1.0], [float("nan")] * 512, [0.0] * 512, [1.0] * 512):
        with pytest.raises(ValueError):
            vector(value)
    video = {"duration_s": 180}
    analysis = {"detections": [{"frame_index": i, "at_s": i / 2} for i in range(360)]}
    assert len(samples_for(video, analysis)) == 90


async def test_archive_purge_and_original_changes_are_never_searchable(work):
    manager, directory = work
    with tenant_scope(TENANT):
        await indexed(work)
        (directory / "original.mp4").write_bytes(b"changed recording")
        stale = await manager.query(SearchRequest(text="first"), OPERATOR)
        assert stale["results"] == [] and stale["skipped_recordings"] == 1
        assert (await manager.catalog(OPERATOR))["videos"][0]["index"]["status"] == "stale"
        video = await manager.store.get(TENANT, "video", VIDEO)
        await manager.store.put(TENANT, "video", VIDEO, {**video, "archived_at": "now"})
        assert (await manager.catalog(OPERATOR))["videos"] == []
        with pytest.raises(HTTPException) as error:
            await manager.enqueue(request(), OPERATOR)
        assert error.value.status_code == 409
        await manager.remove(VIDEO, OPERATOR)
        assert not (directory / INDEX_NAME).exists()
        assert (directory / "original.mp4").exists()


async def test_changed_video_or_analysis_revision_is_stale_without_latest_pointer_substitution(
    work,
):
    manager, _ = work
    with tenant_scope(TENANT):
        await indexed(work)
        run = await manager.store.get(TENANT, "analysis", ANALYSIS)
        await manager.store.put(TENANT, "analysis", ANALYSIS, {**run, "events": []})
        assert (await manager.catalog(OPERATOR))["videos"][0]["index"]["status"] == "stale"
        result = await manager.query(SearchRequest(text="first"), OPERATOR)
        assert result["results"] == []
        with pytest.raises(HTTPException) as error:
            await manager.query(
                SearchRequest(
                    sample=SampleReference(video_id=VIDEO, analysis_id=ANALYSIS, frame_index=0)
                ),
                OPERATOR,
            )
        assert error.value.status_code == 409


async def test_tenant_and_all_source_zones_are_required_and_index_metadata_is_hidden(work):
    manager, _ = work
    wrong = Principal(
        subject="reviewer", tenant_id="other", roles=frozenset({"admin"}), clearance=3
    )
    narrow = Principal(
        subject="reviewer",
        tenant_id=TENANT,
        roles=frozenset({"operator"}),
        clearance=3,
        zones=frozenset({"gate"}),
    )
    with tenant_scope(TENANT):
        await indexed(work)
        with pytest.raises(HTTPException) as error:
            await manager.query(SearchRequest(text="first"), wrong)
        assert error.value.status_code == 401
        run = await manager.store.get(TENANT, "analysis", ANALYSIS)
        await manager.store.put(
            TENANT, "analysis", ANALYSIS, {**run, "rules": [{"zone_id": "secret"}]}
        )
        with pytest.raises(HTTPException) as error:
            await manager.enqueue(request(), narrow)
        assert error.value.status_code == 403
        assert (await manager.query(SearchRequest(text="first"), narrow))["results"] == []
        # An accessible alternative analysis must not expose the restricted index's identity.
        await manager.store.put(
            TENANT, "analysis", "ana_" + "c" * 32, {**run, "analysis_id": "ana_" + "c" * 32}
        )
        assert (await manager.catalog(narrow))["videos"][0]["index"] is None
    with tenant_scope("other"):
        assert (await manager.catalog(wrong))["videos"] == []


async def test_model_missing_never_falls_back_or_changes_source(work, monkeypatch):
    manager, _ = work
    monkeypatch.setattr(
        module, "check_model", lambda _: {"available": False, "reason": "model missing"}
    )
    before = deepcopy(manager.store.records)
    with tenant_scope(TENANT):
        with pytest.raises(HTTPException) as error:
            await manager.enqueue(request(), OPERATOR)
        assert error.value.status_code == 503
        with pytest.raises(HTTPException) as error:
            await manager.query(SearchRequest(text="first"), OPERATOR)
        assert error.value.status_code == 503
    assert manager.store.records == before


async def test_queued_index_cannot_be_deleted_and_second_enqueue_is_bounded(work):
    manager, _ = work
    with tenant_scope(TENANT):
        async with video_processor_lock(manager.store):
            await manager.enqueue(request(), OPERATOR)
            with pytest.raises(HTTPException) as error:
                await manager.remove(VIDEO, OPERATOR)
            assert error.value.status_code == 409
            with pytest.raises(HTTPException) as error:
                await manager.enqueue(request(), OPERATOR)
            assert error.value.status_code == 409
        await manager.task
        assert (await manager.store.get(TENANT, KIND, VIDEO))["status"] == "ready"


async def test_index_lifecycle_holds_video_lock_until_native_read_finishes(work, monkeypatch):
    manager, _ = work
    entered, release = asyncio.Event(), asyncio.Event()
    original = module.blocking_media

    async def held(function, *args):
        if getattr(function, "__name__", "") == "sample":
            entered.set()
            await release.wait()
        return await original(function, *args)

    monkeypatch.setattr(module, "blocking_media", held)
    with tenant_scope(TENANT):
        await manager.enqueue(request(), OPERATOR)
        await asyncio.wait_for(entered.wait(), 5)
        lock = video_mutation_lock(manager.store, TENANT, VIDEO)
        assert lock.locked()
        with pytest.raises(HTTPException) as error:
            await manager.query(SearchRequest(text="first"), OPERATOR)
        assert error.value.status_code == 409
        release.set()
        await manager.task
        assert not lock.locked()


async def test_recovery_requires_explicit_retry_and_corrupt_index_never_returns_vectors(work):
    manager, directory = work
    with tenant_scope(TENANT):
        await manager.store.put(
            TENANT, KIND, VIDEO, {"video_id": VIDEO, "analysis_id": ANALYSIS, "status": "running"}
        )
        await manager.start()
        assert (await manager.store.get(TENANT, KIND, VIDEO))["status"] == "interrupted"
        await indexed(work)
        (directory / INDEX_NAME).write_text('{"samples":[{"vector":[0]}]}')
        assert (await manager.query(SearchRequest(text="first"), OPERATOR))["results"] == []
        await manager.close()


async def test_original_and_index_symlinks_are_refused(work, tmp_path):
    manager, directory = work
    with tenant_scope(TENANT):
        await indexed(work)
        target = tmp_path / "unrelated.json"
        target.write_text((directory / INDEX_NAME).read_text())
        (directory / INDEX_NAME).unlink()
        (directory / INDEX_NAME).symlink_to(target)
        assert (await manager.query(SearchRequest(text="first"), OPERATOR))["results"] == []
        with pytest.raises(HTTPException):
            await manager.remove(VIDEO, OPERATOR)
        assert target.exists()


def test_actual_model_check_rejects_unpinned_assets(tmp_path):
    settings = SimpleNamespace(
        model_dir=tmp_path,
        clip_vision_model="vision",
        clip_text_model="text",
        clip_tokenizer="tokenizer",
    )
    for name in ("vision", "text", "tokenizer"):
        (tmp_path / name).write_bytes(b"not the pinned model")
    assert check_model(settings)["available"] is False


@pytest.mark.models
async def test_real_clip_end_to_end_on_authored_colours(tmp_path):
    """Real ONNX/decoder/storage/query smoke; authored colours are not site accuracy proof."""
    settings = SimpleNamespace(
        tenant_id=TENANT,
        model_dir=MODEL_DIRECTORY,
        clip_vision_model="clip-vision.onnx",
        clip_text_model="clip-text.onnx",
        clip_tokenizer="clip-tokenizer.json",
    )
    if not check_model(settings)["available"]:
        pytest.skip("The pinned local CLIP assets are not installed")
    import cv2
    import numpy as np

    directory = tmp_path / TENANT / VIDEO
    (directory / ANALYSIS).mkdir(parents=True)
    writer = cv2.VideoWriter(
        str(directory / "original.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), 2, (64, 64)
    )
    if not writer.isOpened():
        pytest.skip("An MP4 encoder is unavailable for this authored fixture")
    try:
        for index in range(8):
            frame = np.full((64, 64, 3), (0, 0, 255) if index < 4 else (255, 0, 0), dtype=np.uint8)
            writer.write(frame)
            assert cv2.imwrite(str(directory / ANALYSIS / f"{index}.jpg"), frame)
    finally:
        writer.release()
    store = MemoryStore()
    await store.put(
        TENANT,
        "video",
        VIDEO,
        {
            "video_id": VIDEO,
            "duration_s": 4,
            "status": "completed",
            "title": "Authored red then blue",
        },
    )
    await store.put(
        TENANT,
        "analysis",
        ANALYSIS,
        {
            "analysis_id": ANALYSIS,
            "video_id": VIDEO,
            "status": "completed",
            "detections": [{"frame_index": i, "at_s": i / 2, "objects": []} for i in range(8)],
            "events": [],
        },
    )
    manager = RecordedSearch(
        settings,
        store,
        SimpleNamespace(directory=lambda tenant, video_id: tmp_path / tenant / video_id),
    )
    try:
        with tenant_scope(TENANT):
            await manager.enqueue(request(), OPERATOR)
            await manager.task
            row = await store.get(TENANT, KIND, VIDEO)
            assert row["status"] == "ready", row.get("error")
            red = await manager.query(SearchRequest(text="a red image"), OPERATOR)
            blue = await manager.query(SearchRequest(text="a blue image"), OPERATOR)
            assert red["model_id"] == MODEL_ID
            assert red["results"][0]["at_s"] == 0
            assert blue["results"][0]["at_s"] == 2
    finally:
        await manager.close()
