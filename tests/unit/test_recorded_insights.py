"""Recorded insight search and reports retain source scope and lifecycle integrity."""

import asyncio
import csv
import io
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sio_api.recorded_insights import (
    KIND,
    CountingLine,
    MovementConfiguration,
    MovementCreate,
    MovementRequest,
    ObjectQuery,
    RecordedInsights,
    install_recorded_insight_routes,
    movement_csv,
)
from sio_api.video_storage import VideoRevision, VideoStorage
from test_cases import MemoryStore
from test_review_backup import backup

from sio_core.authn import DevJwtAuth, Principal
from sio_core.guard import install_governance
from sio_core.tenancy import tenant_scope

TENANT = "tenant-a"
VIDEO = "vid_" + "a" * 32
ANALYSIS = "ana_" + "b" * 32
ALICE = Principal(subject="alice", tenant_id=TENANT, roles=frozenset({"operator"}), clearance=3)


@pytest.fixture
async def work(tmp_path):
    store = MemoryStore()
    zone = {"zone_id": "gate", "name": "Gate", "points": [[0, 0], [1, 0], [1, 1], [0, 1]]}
    await store.put(
        TENANT,
        "video",
        VIDEO,
        {
            "video_id": VIDEO,
            "title": "Authored recording",
            "duration_s": 2,
            "status": "completed",
            "zones": [zone],
            "rules": [],
            "analysis_id": "newer-failed-run",
            "privacy": "full_frame_pixelation",
        },
    )
    frames = [
        {
            "at_s": i / 2,
            "frame_index": i,
            "frame_url": "https://untrusted.invalid/image",
            "objects": [
                {
                    "track_id": "region-1",
                    "class_name": "person",
                    "confidence": 0.9,
                    "bbox": [0.4, y - 0.2, 0.6, y],
                },
                {
                    "track_id": "region-2",
                    "class_name": "bus",
                    "confidence": 0.8,
                    "bbox": [0.7, 0.1, 0.9, 0.3],
                },
            ],
        }
        for i, y in enumerate([0.3, 0.4, 0.7])
    ]
    await store.put(
        TENANT,
        "analysis",
        ANALYSIS,
        {
            "analysis_id": ANALYSIS,
            "video_id": VIDEO,
            "status": "completed",
            "zones": [zone],
            "rules": [],
            "model": {"mode": "onnx", "name": "authored", "weights_sha256": "abc", "sample_fps": 2},
            "detections": frames,
            "events": [],
        },
    )
    await store.put(
        TENANT,
        "analysis",
        "newer-failed-run",
        {"analysis_id": "newer-failed-run", "video_id": VIDEO, "status": "failed", "zones": [zone]},
    )

    async def jobs(_tenant):
        return []

    def directory(tenant, video_id):
        return tmp_path / tenant / video_id

    directory(TENANT, VIDEO).mkdir(parents=True)
    for name in ("original.mp4", "playback.mp4", "poster.jpg"):
        (directory(TENANT, VIDEO) / name).write_bytes(b"authored fixture")
    return RecordedInsights(store), VideoStorage(
        SimpleNamespace(store=store, jobs=jobs, directory=directory)
    )


def body(**changes):
    return MovementCreate(
        analysis_id=ANALYSIS,
        title="Gate count",
        configuration=MovementConfiguration(
            line=CountingLine(name="Gate boundary", start=(0.2, 0.5), end=(0.8, 0.5))
        ),
        **changes,
    )


async def test_search_latest_completed_not_failed_pointer_and_paginated_exact_jumps(work):
    manager, _ = work
    with tenant_scope(TENANT):
        catalog = await manager.catalog(ALICE)
        assert catalog["videos"][0]["analyses"][0]["analysis_id"] == ANALYSIS
        assert catalog["videos"][0]["analyses"][0]["classes"] == ["bus", "person"]
        first = await manager.objects(ObjectQuery(limit=1), ALICE)
        second = await manager.objects(ObjectQuery(limit=1, offset=1), ALICE)
        assert first["count"] == second["count"] == 2
        assert first["possibly_truncated"] and not first["scan_truncated"]
        assert not second["possibly_truncated"]
        assert first["results"][0]["track_id"] != second["results"][0]["track_id"]
        filtered = await manager.objects(
            ObjectQuery(
                video_id=VIDEO,
                analysis_id=ANALYSIS,
                class_name="person",
                zone_id="gate",
                start_s=0.5,
                end_s=1,
                min_confidence=0.85,
            ),
            ALICE,
        )
        track = filtered["results"][0]
        assert track["first_s"] == 0.5 and track["last_s"] == 1 and track["sample_count"] == 2
        assert track["frame_url"] == f"/api/review/videos/{VIDEO}/frames/{ANALYSIS}/1"
        assert all(point["frame_url"].startswith("/api/review/") for point in track["observations"])


async def test_preview_is_read_only_save_idempotent_immutable_and_reloadable(work):
    manager, _ = work
    before = deepcopy(manager.store.records)
    with tenant_scope(TENANT):
        preview = await manager.preview(
            VIDEO, MovementRequest(**body().model_dump(exclude={"title"})), ALICE
        )
        assert manager.store.records == before
        assert preview["totals"] == {"a_to_b": 1, "b_to_a": 0, "total": 1}
        assert preview["peak_count"] == 2 and preview["sample_count"] == 3
        one, two = await asyncio.gather(
            manager.create_report(VIDEO, body(), ALICE), manager.create_report(VIDEO, body(), ALICE)
        )
        assert one == two and one["revision"] == 1
        assert one["model"]["weights_sha256"] == "abc"
        restored = RecordedInsights(manager.store)
        assert (await restored.reports(VIDEO, ANALYSIS, ALICE))["reports"] == [one]
        assert await restored.report(one["report_id"], ALICE) == one
        assert all(manager.store.records[key] == value for key, value in before.items())


async def test_metadata_catalog_avoids_loading_frames_and_search_fetches_exact_source(work):
    manager, _ = work
    run = await manager.store.get(TENANT, "analysis", ANALYSIS)

    async def metadata(tenant, limit):
        assert tenant == TENANT and limit == 500
        return [
            {
                **{key: value for key, value in run.items() if key not in {"detections", "events"}},
                "detected_classes": ["bus", "person"],
                "sample_count": 3,
            }
        ]

    manager.store.list_analysis_metadata = metadata
    with tenant_scope(TENANT):
        catalog = await manager.catalog(ALICE)
        assert catalog["videos"][0]["analyses"][0]["classes"] == ["bus", "person"]
        page = await manager.objects(ObjectQuery(), ALICE)
        assert page["count"] == 2 and page["results"][0]["sample_count"] == 3


async def test_cumulative_search_budget_is_disclosed_not_reported_as_exhaustive(work, monkeypatch):
    import sio_api.recorded_insights as module

    manager, _ = work
    monkeypatch.setattr(module, "OBSERVATION_SCAN", 5)
    with tenant_scope(TENANT):
        page = await manager.objects(ObjectQuery(limit=1), ALICE)
        assert page["count"] == 0 and page["scan_truncated"] and page["possibly_truncated"]


@pytest.mark.parametrize(
    "state",
    [
        {"archived_at": "now"},
        {"purge_started_at": "now"},
        {"status": "purge_failed"},
        {"media_available": False},
    ],
)
async def test_unavailable_recordings_not_searchable_or_exportable(work, state):
    manager, _ = work
    with tenant_scope(TENANT):
        saved = await manager.create_report(VIDEO, body(), ALICE)
        video = await manager.store.get(TENANT, "video", VIDEO)
        await manager.store.put(TENANT, "video", VIDEO, {**video, **state})
        assert (await manager.catalog(ALICE))["videos"] == []
        assert (await manager.objects(ObjectQuery(), ALICE))["results"] == []
        for action in (
            manager.report(saved["report_id"], ALICE),
            manager.create_report(VIDEO, body(), ALICE),
        ):
            with pytest.raises(HTTPException) as error:
                await action
            assert error.value.status_code == 409


async def test_current_and_retained_source_scopes_and_tenant_apply_to_all_reads(work):
    manager, _ = work
    narrow = Principal(
        subject="alice",
        tenant_id=TENANT,
        roles=frozenset({"operator"}),
        clearance=3,
        zones=frozenset({"gate"}),
    )
    with tenant_scope(TENANT):
        saved = await manager.create_report(VIDEO, body(), ALICE)
        run = await manager.store.get(TENANT, "analysis", ANALYSIS)
        await manager.store.put(
            TENANT, "analysis", ANALYSIS, {**run, "rules": [{"zone_id": "restricted"}]}
        )
        assert (await manager.catalog(narrow))["videos"] == []
        assert (await manager.objects(ObjectQuery(), narrow))["results"] == []
        for action in (
            manager.report(saved["report_id"], narrow),
            manager.objects(ObjectQuery(video_id=VIDEO, analysis_id=ANALYSIS), narrow),
            manager.create_report(VIDEO, body(), narrow),
        ):
            with pytest.raises(HTTPException) as error:
                await action
            assert error.value.status_code == 403
    other = Principal(subject="alice", tenant_id="other", roles=frozenset({"admin"}), clearance=3)
    with tenant_scope("other"):
        assert (await manager.catalog(other))["videos"] == []
        with pytest.raises(HTTPException) as error:
            await manager.report(saved["report_id"], other)
        assert error.value.status_code == 404
        with pytest.raises(HTTPException) as error:
            await manager.objects(ObjectQuery(), ALICE)
        assert error.value.status_code == 401


async def test_report_captured_scope_cannot_disappear_when_source_layout_changes(work):
    manager, _ = work
    with tenant_scope(TENANT):
        run = await manager.store.get(TENANT, "analysis", ANALYSIS)
        await manager.store.put(
            TENANT, "analysis", ANALYSIS, {**run, "rules": [{"zone_id": "restricted"}]}
        )
        saved = await manager.create_report(VIDEO, body(), ALICE)
        await manager.store.put(TENANT, "analysis", ANALYSIS, run)
        narrow = Principal(
            subject="alice",
            tenant_id=TENANT,
            roles=frozenset({"operator"}),
            clearance=3,
            zones=frozenset({"gate"}),
        )
        assert (await manager.reports(VIDEO, ANALYSIS, narrow))["reports"] == []
        with pytest.raises(HTTPException) as error:
            await manager.report(saved["report_id"], narrow)
        assert error.value.status_code == 403


@pytest.mark.parametrize("status", ["running", "failed", "cancelled", "interrupted"])
async def test_incomplete_analysis_never_becomes_count_report(work, status):
    manager, _ = work
    run = await manager.store.get(TENANT, "analysis", ANALYSIS)
    await manager.store.put(TENANT, "analysis", ANALYSIS, {**run, "status": status})
    with tenant_scope(TENANT), pytest.raises(HTTPException) as error:
        await manager.create_report(VIDEO, body(), ALICE)
    assert error.value.status_code == 409


async def test_wrong_video_or_unknown_retained_zone_rejected(work):
    manager, _ = work
    with tenant_scope(TENANT):
        with pytest.raises(HTTPException) as error:
            await manager.create_report(
                VIDEO,
                body().model_copy(
                    update={"configuration": MovementConfiguration(zone_id="missing")}
                ),
                ALICE,
            )
        assert error.value.status_code == 422
        run = await manager.store.get(TENANT, "analysis", ANALYSIS)
        await manager.store.put(TENANT, "analysis", ANALYSIS, {**run, "video_id": "another"})
        with pytest.raises(HTTPException) as error:
            await manager.create_report(VIDEO, body(), ALICE)
        assert error.value.status_code == 404


async def test_report_quota_is_serialized_and_deduplicated_existing_still_opens(work, monkeypatch):
    import sio_api.recorded_insights as module

    monkeypatch.setattr(module, "MAX_PER_ANALYSIS", 1)
    manager, _ = work
    with tenant_scope(TENANT):
        saved = await manager.create_report(VIDEO, body(), ALICE)
        assert await manager.create_report(VIDEO, body(), ALICE) == saved
        with pytest.raises(HTTPException) as error:
            await manager.create_report(
                VIDEO, body().model_copy(update={"title": "Second report"}), ALICE
            )
        assert error.value.status_code == 409


async def test_new_report_protects_source_under_same_cleanup_lock(work):
    manager, storage = work
    entered, release = asyncio.Event(), asyncio.Event()
    original = manager.store.put

    async def put(tenant, kind, *args, **kwargs):
        if kind == KIND:
            entered.set()
            await release.wait()
        return await original(tenant, kind, *args, **kwargs)

    manager.store.put = put
    with tenant_scope(TENANT):
        create = asyncio.create_task(manager.create_report(VIDEO, body(), ALICE))
        await asyncio.wait_for(entered.wait(), 2)
        archive = asyncio.create_task(
            storage.archive(TENANT, [VideoRevision(video_id=VIDEO, revision=1)], "alice")
        )
        await asyncio.sleep(0)
        assert not archive.done()
        release.set()
        await create
        with pytest.raises(HTTPException) as error:
            await archive
        assert error.value.status_code == 409
        candidate = (await storage.candidates(TENANT, [VIDEO], manual=True))[0]
        assert candidate["linked_movement_reports"] == 1
        assert "movement_report_linked" in candidate["reasons"]


async def test_backup_references_validate_report_source_and_mismatch(work):
    manager, _ = work
    with tenant_scope(TENANT):
        await manager.create_report(VIDEO, body(), ALICE)
    docs = [
        {"tenant_id": tenant, "kind": kind, "record_id": record_id, "payload": row}
        for (tenant, kind, record_id), row in manager.store.records.items()
    ]
    assert backup.media_references(docs)["missing_records"] == []
    bad = deepcopy(docs)
    report = next(row for row in bad if row["kind"] == KIND)
    report["payload"]["analysis_id"] = "absent"
    assert any(
        "movement_report:" in message and message.endswith(":analysis")
        for message in backup.media_references(bad)["missing_records"]
    )


async def test_csv_contains_exact_provenance_and_escapes_spreadsheet_formulas(work):
    manager, _ = work
    with tenant_scope(TENANT):
        saved = await manager.create_report(
            VIDEO, body().model_copy(update={"title": "=TEST()"}), ALICE
        )
    rows = list(csv.DictReader(io.StringIO(movement_csv(saved))))
    assert len(rows) == 5
    assert rows[0]["kind"] == "summary" and rows[0]["total_crossings"] == "1"
    assert all(
        row["title"] == "'=TEST()"
        and row["analysis_id"] == ANALYSIS
        and row["weights_sha256"] == "abc"
        for row in rows
    )
    crossing = next(row for row in rows if row["kind"] == "observed_crossing")
    assert crossing["from_s"] == "0.5" and crossing["to_s"] == "1.0"


@pytest.mark.parametrize(
    "changes",
    [
        {"analysis_id": ANALYSIS},
        {"zone_id": "gate"},
        {"start_s": 10, "end_s": 2},
        {"min_confidence": float("nan")},
        {"limit": 201},
        {"offset": -1},
        {"offset": 50001},
        {"end_s": 181},
    ],
)
def test_invalid_search_query_rejected(changes):
    with pytest.raises(ValidationError):
        ObjectQuery(**changes)


@pytest.mark.parametrize(
    "start,end",
    [([0, 0], [0, 0]), ([0, 0], [0.001, 0]), ([-1, 0], [1, 1]), ([0, 0], [1, float("inf")])],
)
def test_invalid_counting_geometry_rejected(start, end):
    with pytest.raises(ValidationError):
        CountingLine(name="Boundary", start=start, end=end)


async def test_http_auth_query_contract_preview_and_saved_csv(work, settings):
    manager, _ = work
    settings.audit_enabled = False
    app = FastAPI()
    install_recorded_insight_routes(app, manager.store)
    install_governance(app, service="api", settings=settings)

    def headers(role="operator"):
        return {
            "Authorization": "Bearer "
            + DevJwtAuth(settings).issue(
                subject="alice", tenant_id=TENANT, roles=(role,), clearance=3
            )
        }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        base = f"/api/review/videos/{VIDEO}/movement"
        assert (await client.get("/api/review/objects")).status_code == 401
        response = await client.get(
            "/api/review/objects",
            headers=headers(),
            params={"class_name": "person", "start_s": 0.5},
        )
        assert response.status_code == 200, response.text
        assert response.json()["results"][0]["first_s"] == 0.5
        response = await client.get(
            "/api/review/objects", headers=headers(), params={"offset": 20050}
        )
        assert response.status_code == 200, response.text
        assert response.json()["offset"] == 20050
        assert response.json()["results"] == []
        assert (
            await client.get(
                "/api/review/objects", headers=headers(), params={"analysis_id": ANALYSIS}
            )
        ).status_code == 422
        assert (
            await client.post(base, headers=headers("viewer"), json=body().model_dump())
        ).status_code == 403
        response = await client.post(
            base + "/preview", headers=headers(), json=body().model_dump(exclude={"title"})
        )
        assert response.status_code == 200, response.text
        assert response.json()["totals"]["total"] == 1
        response = await client.post(base, headers=headers(), json=body().model_dump())
        assert response.status_code == 200, response.text
        saved = response.json()
        response = await client.get(
            f"/api/review/movement/{saved['report_id']}/csv", headers=headers()
        )
        assert response.status_code == 200 and "text/csv" in response.headers["content-type"]
        assert response.headers["cache-control"] == "private, no-store"
        assert saved["report_id"] in response.text
