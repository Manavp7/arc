from copy import deepcopy

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sio_api.recording_timeline import (
    RecordingClock,
    RecordingTimeline,
    clock_interval,
    install_recording_timeline_routes,
)
from sio_api.workbench_store import WorkbenchConflict
from test_cases import MemoryStore

from sio_core.authn import DevJwtAuth, Principal
from sio_core.config import Settings
from sio_core.guard import action_for, install_governance
from sio_core.tenancy import tenant_scope

TENANT = "timeline-test"
VIDEO = "vid_" + "a" * 32
ALICE = Principal(subject="alice", tenant_id=TENANT, roles=frozenset({"operator"}), clearance=3)


class TimelineStore(MemoryStore):
    async def list_analysis_metadata(self, tenant, limit=500):
        return await self.list(tenant, "analysis", limit)


def clock(**updates):
    return RecordingClock(
        **{
            "revision": 0,
            "camera_id": "dock-1",
            "capture_started_at": "2026-09-23T14:05:00+05:30",
            "clock_offset_s": -2,
            "uncertainty_s": 0.5,
            "note": "Camera clock compared against recorder",
            **updates,
        }
    )


@pytest.fixture
async def work():
    store = TimelineStore()
    await store.put(
        TENANT,
        "video",
        VIDEO,
        {
            "video_id": VIDEO,
            "title": "Dock",
            "duration_s": 10,
            "zones": [],
            "status": "ready",
        },
    )
    await store.put(
        TENANT,
        "analysis",
        "ana_old",
        {
            "video_id": VIDEO,
            "analysis_id": "ana_old",
            "status": "completed",
            "zones": [],
            "created_at": "2026-09-22T00:00:00+00:00",
        },
    )
    await store.put(
        TENANT,
        "analysis",
        "ana_new_failed",
        {
            "video_id": VIDEO,
            "analysis_id": "ana_new_failed",
            "status": "failed",
            "zones": [],
        },
    )
    return store, RecordingTimeline(store)


@pytest.mark.parametrize(
    "updates",
    [
        {"capture_started_at": "2026-09-23T14:05:00"},
        {"clock_offset_s": float("nan")},
        {"uncertainty_s": -1},
        {"note": "  "},
        {"camera_id": ""},
        {"capture_started_at": None},
        {"clock_offset_s": 86401},
    ],
)
def test_invalid_or_undocumented_clocks_are_rejected(updates):
    with pytest.raises(ValidationError):
        clock(**updates)


def test_timezone_and_signed_correction_do_not_use_upload_time():
    result = clock_interval(clock().model_dump(mode="json"), 10)
    assert result == {"start": "2026-09-23T08:34:58+00:00", "end": "2026-09-23T08:35:08+00:00"}
    assert clock_interval(None, 10) is None


async def test_unknown_clocks_stay_separate_and_updates_preserve_evidence(work):
    store, manager = work
    original = deepcopy(store.records)
    with tenant_scope(TENANT):
        result = await manager.timeline(ALICE)
        assert result["recordings"][0]["interval"] is None
        assert result["recordings"][0]["analysis_id"] == "ana_old"
        saved = await manager.save_clock(VIDEO, clock(), ALICE)
        assert saved["declared_by"] == "alice"
        assert saved["basis"] == "operator_declared"
        with pytest.raises(WorkbenchConflict):
            await manager.save_clock(VIDEO, clock(), ALICE)
        result = await manager.timeline(ALICE)
        assert result["recordings"][0]["interval"]["start"] == "2026-09-23T08:34:58+00:00"
        await manager.save_clock(
            VIDEO, RecordingClock(revision=saved["revision"], camera_id="dock-1"), ALICE
        )
        assert (await manager.timeline(ALICE))["recordings"][0]["interval"] is None
    assert all(store.records[key] == value for key, value in original.items())


async def test_archived_and_other_tenant_footage_is_unavailable(work):
    store, manager = work
    record = await store.get(TENANT, "video", VIDEO)
    await store.put(TENANT, "video", VIDEO, {**record, "archived_at": "2026-09-23T00:00:00Z"})
    with tenant_scope(TENANT):
        assert (await manager.timeline(ALICE))["recordings"] == []
        with pytest.raises(HTTPException) as error:
            await manager.save_clock(VIDEO, clock(), ALICE)
        assert error.value.status_code == 409
    with tenant_scope("other"):
        with pytest.raises(HTTPException) as error:
            await manager.timeline(ALICE)
        assert error.value.status_code == 401


async def test_clock_http_permissions_and_no_forged_authorship(work, tmp_path):
    store, _ = work
    cfg = Settings(_env_file=None, tenant_id=TENANT, data_dir=tmp_path, auth_required=True)
    app = FastAPI()
    install_recording_timeline_routes(app, store)
    install_governance(app, settings=cfg, service="api")
    auth = DevJwtAuth(cfg)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/review/recording-timeline")).status_code == 401
        viewer = auth.issue(subject="viewer", tenant_id=TENANT, roles=("viewer",), clearance=3)
        response = await client.put(
            f"/api/review/videos/{VIDEO}/clock",
            json=clock().model_dump(mode="json"),
            headers={"Authorization": f"Bearer {viewer}"},
        )
        assert response.status_code == 403
        token = auth.issue(subject="alice", tenant_id=TENANT, roles=("operator",), clearance=3)
        response = await client.put(
            f"/api/review/videos/{VIDEO}/clock",
            json={**clock().model_dump(mode="json"), "declared_by": "forged"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422


async def test_existing_clock_scope_is_required_before_replacement(work):
    store, manager = work
    await store.put(TENANT, "recording_clock", VIDEO, {"zones": [{"zone_id": "restricted"}]})
    scoped = Principal(
        subject="scoped",
        tenant_id=TENANT,
        roles=frozenset({"operator"}),
        clearance=3,
        zones=frozenset({"dock"}),
    )
    with tenant_scope(TENANT):
        assert (await manager.timeline(scoped))["recordings"] == []
        with pytest.raises(HTTPException) as error:
            await manager.save_clock(VIDEO, clock(revision=1), scoped)
        assert error.value.status_code == 403
    assert (await store.get(TENANT, "recording_clock", VIDEO))["zones"] == [
        {"zone_id": "restricted"}
    ]


def test_search_is_read_but_delivery_retry_and_activation_require_integration_write():
    assert action_for("POST", "/api/review/recorded-search/query") == "review.read"
    assert action_for("POST", "/api/review/recorded-search/index") == "review.write"
    assert action_for("POST", "/api/alert-deliveries/id/retry") == "integration.write"
    assert action_for("POST", "/sources/cam/activation") == "integration.write"
