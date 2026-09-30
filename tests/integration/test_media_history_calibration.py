"""Real SQL history, worker exclusion and reviewed calibration in disposable schemas."""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException
from psycopg.conninfo import make_conninfo
from sio_api.calibration_publication import (
    ApplyCalibration,
    CalibrationPublication,
    PreviewCalibration,
)
from sio_api.camera_commissioning import CameraSetup, validate_measurements
from sio_api.evidence_packages import EvidencePackageManager
from sio_api.media_coordination import WorkerPresence, claim, shared_lock
from sio_api.recorded_insights import RecordedInsights
from sio_api.workbench_store import WorkbenchStore
from sio_fusion.service import FusionService

from sio_core import PgPool
from sio_core.authn import Principal
from sio_core.config import Settings
from sio_core.tenancy import tenant_scope

pytestmark = pytest.mark.infra
ROOT = Path(__file__).resolve().parents[2]
TENANT = "media-test"
ADMIN = Principal(subject="calibrator", tenant_id=TENANT, roles=frozenset({"admin"}), clearance=3)


@pytest.fixture
async def database(tmp_path):
    cfg = Settings(data_dir=tmp_path, tenant_id=TENANT, bus_backend="memory")
    schema = "media_test_" + uuid4().hex
    control = await psycopg.AsyncConnection.connect(cfg.pg_dsn, autocommit=True)
    pool = PgPool(
        make_conninfo(cfg.pg_dsn, options=f"-c search_path={schema},public -c client_encoding=UTF8")
    )
    try:
        await control.execute(f"CREATE SCHEMA {schema}")
        await pool.open()
        await pool.execute("CREATE TABLE tenants (tenant_id text PRIMARY KEY)")
        await pool.execute("INSERT INTO tenants VALUES (%s)", (TENANT,))
        await pool.execute("CREATE TABLE sources (LIKE public.sources INCLUDING ALL)")
        for name in (
            "008_workbench.sql",
            "012_workbench_history.sql",
            "013_camera_calibration.sql",
            "014_workbench_references.sql",
        ):
            await pool.execute((ROOT / "infra/postgres" / name).read_text())
        yield cfg, pool, WorkbenchStore(pool)
    finally:
        await pool.close()
        await control.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await control.close()


def measured_setup():
    points = []
    for i, (x, y) in enumerate(((0.2, 0.4), (0.8, 0.4), (0.5, 0.8))):
        distance = 6 / math.tan(math.radians(35 + (y - 0.5) * 45))
        angle = math.radians((x - 0.5) * 70)
        points.append(
            {
                "checkpoint_id": f"p{i}",
                "label": f"Point {i}",
                "image_x": x,
                "image_y": y,
                "measured_east_m": distance * math.sin(angle),
                "measured_north_m": distance * math.cos(angle),
            }
        )
    return CameraSetup(
        title="Authored calibration",
        source_id="camera-a",
        site_id="site",
        camera_id="camera",
        pose={
            "lat": 23.25,
            "lon": 77.41,
            "bearing_deg": 0,
            "height_m": 6,
            "tilt_deg": 35,
            "fov_deg": 70,
            "vfov_deg": 45,
            "frame_width": 1920,
            "frame_height": 1080,
            "range_m": 60,
        },
        checkpoints=points,
        tolerance_m=0.1,
        measurement_note="Synthetic geometry, not a field survey",
    )


async def save_setup(store):
    setup = measured_setup()
    return await store.put(
        TENANT,
        "camera_setup",
        "setup",
        {
            **setup.model_dump(mode="json"),
            "setup_id": "setup",
            "status": "validated",
            "validation": validate_measurements(setup),
        },
        expected_revision=0,
    )


async def test_calibration_preview_cas_acknowledgement_and_rollback(database):
    cfg, pool, store = database
    setup = await save_setup(store)
    manager = CalibrationPublication(store, pool)
    with tenant_scope(TENANT):
        preview = await manager.preview(
            "setup", PreviewCalibration(expected_revision=setup["revision"]), ADMIN
        )
        assert "source_digest" not in preview and "actor" not in preview
        assert await pool.fetchval("SELECT count(*) FROM sources") == 0
        body = ApplyCalibration(
            preview_id=preview["preview_id"], expected_revision=setup["revision"]
        )
        applied = await manager.apply("setup", body, ADMIN)
        assert applied["status"] == "pending_fusion"
        with pytest.raises(HTTPException) as error:
            await manager.apply("setup", body, ADMIN)
        assert error.value.status_code == 409
        fusion = FusionService(cfg)
        fusion.pool = pool
        await fusion._load_calibrations()
        assert (await manager.status("setup", ADMIN))["status"] == "applied"
        assert "camera-a" in fusion.projectors
        rollback = await manager.preview(
            "setup", PreviewCalibration(expected_revision=setup["revision"]), ADMIN, rollback=True
        )
        await manager.apply(
            "setup",
            ApplyCalibration(
                preview_id=rollback["preview_id"], expected_revision=setup["revision"]
            ),
            ADMIN,
            rollback=True,
        )
        await fusion._load_calibrations()
        assert (await manager.status("setup", ADMIN))["status"] == "rolled_back"
        assert "camera-a" not in fusion.projectors
        assert await pool.fetchval("SELECT count(*) FROM camera_calibration_changes") == 2


async def test_calibration_rebound_source_stale_ticket_and_actor_are_rejected(database):
    _, pool, store = database
    setup = await save_setup(store)
    manager = CalibrationPublication(store, pool)
    with tenant_scope(TENANT):
        preview = await manager.preview("setup", PreviewCalibration(expected_revision=1), ADMIN)
        body = ApplyCalibration(preview_id=preview["preview_id"], expected_revision=1)
        with pytest.raises(HTTPException) as error:
            await manager.apply("setup", body, replace(ADMIN, subject="another"))
        assert error.value.status_code == 403
        await manager.apply("setup", body, ADMIN)
        await pool.execute(
            "INSERT INTO sources(tenant_id,source_id,kind,modality,calibration_revision) VALUES (%s,'camera-b','camera','video',1)",
            (TENANT,),
        )
        await store.put(
            TENANT, "camera_setup", "setup", {**setup, "source_id": "camera-b"}, expected_revision=1
        )
        assert (await manager.status("setup", ADMIN))["can_rollback"] is False
        with pytest.raises(HTTPException) as error:
            await manager.preview(
                "setup", PreviewCalibration(expected_revision=2), ADMIN, rollback=True
            )
        assert error.value.status_code == 409
        assert await pool.fetchval("SELECT config FROM sources WHERE source_id='camera-b'") == {}


async def test_distinct_workers_share_lifecycle_claim_and_lost_claim_cannot_write(database):
    _, pool, store = database
    other = WorkbenchStore(pool)
    async with claim(store, "authored", TENANT) as first:
        assert first
        async with claim(other, "authored", TENANT) as second:
            assert not second
        await store.put(TENANT, "note", "one", {"value": 1})
        lock = shared_lock(store, "authored", TENANT)
        await lock.connection.close()
        with pytest.raises(psycopg.OperationalError):
            await store.put(TENANT, "note", "two", {"value": 2})
        with pytest.raises(psycopg.OperationalError):
            await store.delete(TENANT, "note", "one", expected_revision=1)
    assert await store.get(TENANT, "note", "two") is None
    assert (await store.get(TENANT, "note", "one"))["value"] == 1
    async with claim(other, "authored", TENANT) as recovered:
        assert recovered
        assert await other.delete(TENANT, "note", "one", expected_revision=1)
    assert await store.get(TENANT, "note", "one") is None


async def test_other_worker_does_not_interrupt_live_queued_export(database):
    cfg, pool, store = database
    owner = WorkerPresence(store)
    identifier = await owner.start()
    package = "pkg_" + "a" * 32
    await store.put(
        TENANT,
        "evidence_package",
        package,
        {"package_id": package, "status": "queued", "worker_id": identifier},
    )
    observer = EvidencePackageManager(cfg, WorkbenchStore(pool), pool)
    try:
        assert (await observer.load(TENANT, package))["status"] == "queued"
        await owner.close()
        assert (await observer.load(TENANT, package))["status"] == "interrupted"
    finally:
        await owner.close()


async def test_worker_presence_recovers_with_new_identity_after_connection_loss(database):
    _, _, store = database
    owner = WorkerPresence(store)
    first = await owner.start()
    await owner.connection.close()
    second = await owner.start()
    try:
        assert first != second
        assert not await owner.alive(first)
        assert await owner.alive(second)
    finally:
        await owner.close()


async def test_rollback_to_invalid_old_pose_receives_fusion_ack(database):
    cfg, pool, store = database
    await pool.execute(
        "INSERT INTO sources(tenant_id,source_id,kind,modality,geom,config) VALUES (%s,'camera-a','camera','video',ST_SetSRID(ST_MakePoint(77,23),4326),'{\"frame_width\":0}'::jsonb)",
        (TENANT,),
    )
    await save_setup(store)
    manager = CalibrationPublication(store, pool)
    with tenant_scope(TENANT):
        preview = await manager.preview("setup", PreviewCalibration(expected_revision=1), ADMIN)
        await manager.apply(
            "setup", ApplyCalibration(preview_id=preview["preview_id"], expected_revision=1), ADMIN
        )
        rollback = await manager.preview(
            "setup", PreviewCalibration(expected_revision=1), ADMIN, rollback=True
        )
        await manager.apply(
            "setup",
            ApplyCalibration(preview_id=rollback["preview_id"], expected_revision=1),
            ADMIN,
            rollback=True,
        )
        fusion = FusionService(cfg)
        fusion.pool = pool
        await fusion._load_calibrations()
        assert (await manager.status("setup", ADMIN))["status"] == "rolled_back"
        assert "camera-a" not in fusion.projectors


async def test_history_pagination_and_per_recording_analyses_ignore_other_recording_volume(
    database,
):
    _, pool, store = database
    await store.put(
        TENANT, "video", "old", {"video_id": "old", "status": "ready", "duration_s": 5, "zones": []}
    )
    await pool.execute(
        """INSERT INTO workbench_documents(tenant_id,kind,record_id,payload)
        SELECT %s,'analysis','old-'||lpad(i::text,4,'0'),jsonb_build_object('video_id','old','analysis_id','old-'||lpad(i::text,4,'0'),'status','completed','zones','[]'::jsonb) FROM generate_series(1,520) i""",
        (TENANT,),
    )
    await pool.execute(
        """INSERT INTO workbench_documents(tenant_id,kind,record_id,payload)
        SELECT %s,'analysis','other-'||i,jsonb_build_object('video_id','other','analysis_id','other-'||i,'status','completed') FROM generate_series(1,520) i""",
        (TENANT,),
    )
    manager = RecordedInsights(store)
    with tenant_scope(TENANT):
        one = await manager.retained_analyses("old", ADMIN, limit=500)
        two = await manager.retained_analyses("old", ADMIN, limit=500, cursor=one["next_cursor"])
        ids = [row["analysis_id"] for page in (one, two) for row in page["analyses"]]
        assert len(ids) == len(set(ids)) == 520
        assert two["next_cursor"] is None
        candidates, truncated = await manager.candidates(ADMIN, "old")
        assert candidates[0][0]["video_id"] == "old" and truncated
    assert (
        await pool.fetchval("SELECT count(*) FROM workbench_revisions WHERE kind='analysis'")
        == 1040
    )


async def test_reference_index_protects_nested_exact_analysis_and_tracks_updates(database):
    _, pool, store = database
    video, analysis = "vid_" + "a" * 32, "ana_" + "b" * 32
    await store.put(TENANT, "video", video, {"video_id": video, "status": "ready"})
    await store.put(TENANT, "analysis", analysis, {"video_id": video, "analysis_id": analysis})
    record = await store.put(
        TENANT,
        "case",
        "case-one",
        {"attachments": [{"source": {"analysis_id": analysis}}, {"video_id": video}]},
    )
    assert (await store.reference_counts(TENANT, [video]))[video] == {"case": 1}
    await store.put(
        TENANT, "case", "case-one", {"attachments": []}, expected_revision=record["revision"]
    )
    assert await store.reference_counts(TENANT, [video]) == {}
    assert await pool.fetchval("SELECT count(*) FROM workbench_revisions WHERE kind='case'") == 2
