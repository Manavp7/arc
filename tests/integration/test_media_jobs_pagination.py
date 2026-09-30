"""SQL queue history stays reachable and indexed references protect retained media."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from psycopg.conninfo import make_conninfo
from sio_api.video_review import VideoReviewManager
from sio_api.video_storage import install_storage_routes
from sio_api.workbench_store import WorkbenchStore

from sio_core import PgPool
from sio_core.authn import DevJwtAuth
from sio_core.config import Settings
from sio_core.guard import install_governance

pytestmark = pytest.mark.infra
ROOT = Path(__file__).resolve().parents[2]
TENANT = "jobs-pagination-test"
VIDEO = "vid_" + "a" * 32
OTHER_VIDEO = "vid_" + "b" * 32
ANALYSIS = "ana_" + "a" * 32
OTHER_ANALYSIS = "ana_" + "b" * 32
MEMBERSHIP = {"zones": [{"zone_id": "allowed"}]}


@pytest.fixture
async def database(tmp_path):
    cfg = Settings(data_dir=tmp_path, tenant_id=TENANT, bus_backend="memory", audit_enabled=False)
    schema = "jobs_pagination_" + uuid4().hex
    control = await psycopg.AsyncConnection.connect(cfg.pg_dsn, autocommit=True)
    pool = PgPool(
        make_conninfo(cfg.pg_dsn, options=f"-c search_path={schema},public -c client_encoding=UTF8")
    )
    try:
        await control.execute(f"CREATE SCHEMA {schema}")
        await pool.open()
        await pool.execute("CREATE TABLE tenants (tenant_id text PRIMARY KEY)")
        await pool.execute("INSERT INTO tenants VALUES (%s),(%s)", (TENANT, "other-tenant"))
        for name in ("008_workbench.sql", "012_workbench_history.sql"):
            await pool.execute((ROOT / "infra/postgres" / name).read_text())
        store = WorkbenchStore(pool)
        manager = VideoReviewManager(cfg, store)
        # These tests exercise reads of already recovered data, without starting a decoder.
        manager.recovered_tenants.add(TENANT)
        manager.closing = True
        app = FastAPI()
        install_storage_routes(app, manager)
        install_governance(app, service="api", settings=cfg)
        yield cfg, pool, store, manager, app
    finally:
        await pool.close()
        await control.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await control.close()


async def seed_video(store, video_id=VIDEO, *, tenant=TENANT):
    return await store.put(
        tenant,
        "video",
        video_id,
        {"video_id": video_id, "title": "Authored recording", "status": "completed", **MEMBERSHIP},
        expected_revision=0,
    )


async def test_active_jobs_survive_finished_history_and_cursor_reaches_oldest(database):
    cfg, pool, store, manager, app = database
    await seed_video(store)
    finished_count = 5001
    # All finished jobs tie on creation time and are more recently updated than the
    # three still-active jobs. The former capped list loses all three active rows.
    await pool.execute(
        """INSERT INTO workbench_documents
            (tenant_id,kind,record_id,payload,created_at,updated_at)
        SELECT %s,'video_job','job_finished_' || lpad(n::text,6,'0'),
            jsonb_build_object('job_id','job_finished_' || lpad(n::text,6,'0'),
                'video_id',%s::text,'status','completed','configuration',%s::jsonb),
            '2026-09-01T00:00:00Z'::timestamptz,'2026-09-01T00:00:00Z'::timestamptz
        FROM generate_series(1,%s) AS jobs(n)""",
        (TENANT, VIDEO, json.dumps(MEMBERSHIP), finished_count),
    )
    active_ids = []
    for index, status in enumerate(("queued", "running", "cancelling")):
        identifier = f"job_active_{index}"
        active_ids.append(identifier)
        await pool.execute(
            """INSERT INTO workbench_documents
                (tenant_id,kind,record_id,payload,created_at,updated_at)
            VALUES (%s,'video_job',%s,%s::jsonb,
                '2025-01-01T00:00:00Z','2025-01-01T00:00:00Z')""",
            (
                TENANT,
                identifier,
                json.dumps(
                    {
                        "job_id": identifier,
                        "video_id": VIDEO,
                        "status": status,
                        "configuration": MEMBERSHIP,
                    }
                ),
            ),
        )
    assert len(await store.list(TENANT, "video_job", limit=5000)) == 5000
    assert {row["job_id"] for row in await manager.jobs(TENANT, active_only=True)} == set(
        active_ids
    )

    token = DevJwtAuth(cfg).issue(subject="reviewer", roles=("operator",), zones=("allowed",))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://authored-test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        active = await client.get("/api/review/jobs", params={"view": "active", "limit": 2})
        assert active.status_code == 200, active.text
        first = active.json()
        second = (
            await client.get(
                "/api/review/jobs",
                params={"view": "active", "limit": 2, "cursor": first["next_cursor"]},
            )
        ).json()
        assert {row["job_id"] for page in (first, second) for row in page["jobs"]} == set(
            active_ids
        )
        assert second["next_cursor"] is None

        seen, cursor = [], ""
        while True:
            response = await client.get("/api/review/jobs", params={"limit": 500, "cursor": cursor})
            assert response.status_code == 200, response.text
            page = response.json()
            seen.extend(row["job_id"] for row in page["jobs"])
            if not cursor:
                # Updating a previously read row must not shift its creation-order page.
                changed = page["jobs"][0]
                await store.put(
                    TENANT, "video_job", changed["job_id"], {**changed, "note": "reviewed"}
                )
            cursor = page["next_cursor"]
            if not cursor:
                break
            assert len(seen) <= finished_count + len(active_ids)
        assert len(seen) == len(set(seen)) == finished_count + len(active_ids)
        assert seen[-3:] == sorted(active_ids, reverse=True)


async def test_comparison_reference_backfill_and_updates_protect_cleanup(database):
    _, pool, store, manager, _ = database
    for video_id, analysis_id in ((VIDEO, ANALYSIS), (OTHER_VIDEO, OTHER_ANALYSIS)):
        await seed_video(store, video_id)
        await store.put(
            TENANT,
            "analysis",
            analysis_id,
            {"analysis_id": analysis_id, "video_id": video_id, "status": "completed", **MEMBERSHIP},
        )
    comparison = await store.put(
        TENANT,
        "case_comparison",
        "comparison-before-migration",
        {"source_refs": [{"analysis_id": ANALYSIS}, {"analysis_id": ANALYSIS}]},
    )
    await pool.execute((ROOT / "infra/postgres/014_workbench_references.sql").read_text())
    assert await store.reference_counts(TENANT, [VIDEO, OTHER_VIDEO]) == {
        VIDEO: {"case_comparison": 1}
    }
    candidate = (await manager.storage.candidates(TENANT, [VIDEO], manual=True))[0]
    assert not candidate["eligible"]
    assert "comparison_linked" in candidate["reasons"]

    await store.put(
        TENANT,
        "case_comparison",
        comparison["record_id"],
        {"source_refs": [{"analysis_id": OTHER_ANALYSIS}]},
        expected_revision=comparison["revision"],
    )
    assert await store.reference_counts(TENANT, [VIDEO, OTHER_VIDEO]) == {
        OTHER_VIDEO: {"case_comparison": 1}
    }
    candidates = {
        row["video_id"]: row for row in await manager.storage.candidates(TENANT, manual=True)
    }
    assert candidates[VIDEO]["eligible"]
    assert "comparison_linked" in candidates[OTHER_VIDEO]["reasons"]
