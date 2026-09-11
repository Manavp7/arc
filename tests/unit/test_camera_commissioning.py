"""Camera readiness validates surveyed inputs without activating or rewriting hardware."""

import math
from copy import deepcopy

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sio_api import camera_commissioning as module
from sio_api.camera_commissioning import (
    CameraSetup,
    install_camera_commissioning_routes,
    validate_measurements,
)
from sio_api.workbench_store import WorkbenchConflict

from sio_core.authn import DevJwtAuth
from sio_core.config import Settings
from sio_core.guard import install_governance
from sio_core.tenancy import current_tenant


class Store:
    def __init__(self):
        self.rows = {}

    async def get(self, tenant, kind, record_id):
        return deepcopy(self.rows.get((tenant, kind, record_id)))

    async def list(self, tenant, kind, limit=500):
        return [
            deepcopy(value)
            for (owner, category, _), value in self.rows.items()
            if owner == tenant and category == kind
        ][:limit]

    async def put(self, tenant, kind, record_id, data, expected_revision=None):
        old = self.rows.get((tenant, kind, record_id))
        revision = old["revision"] if old else 0
        if expected_revision is not None and expected_revision != revision:
            raise WorkbenchConflict("Refresh the current revision")
        result = {
            **deepcopy(data),
            "revision": revision + 1,
            "record_id": record_id,
            "created_at": (old or {}).get("created_at", "2026-09-11T08:00:00Z"),
            "updated_at": "2026-09-11T08:01:00Z",
        }
        self.rows[tenant, kind, record_id] = result
        return deepcopy(result)


def measured_body(**changes):
    points = []
    # Independent fixture geometry: north-facing camera, six metres above flat ground.
    for index, (x, y) in enumerate([(0.2, 0.4), (0.8, 0.4), (0.5, 0.8)]):
        distance = 6 / math.tan(math.radians(35 + (y - 0.5) * 45))
        angle = math.radians((x - 0.5) * 70)
        points.append(
            {
                "checkpoint_id": f"p{index}",
                "label": f"Survey marker {index}",
                "image_x": x,
                "image_y": y,
                "measured_east_m": distance * math.sin(angle),
                "measured_north_m": distance * math.cos(angle),
            }
        )
    return {
        "title": "Gate commissioning",
        "source_id": "rtsp-a",
        "site_id": "site-a",
        "camera_id": "camera-a",
        "pose": {
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
        "checkpoints": points,
        "tolerance_m": 0.1,
        "measurement_note": "Authored synthetic geometry fixture; not a real survey.",
        "expected_revision": 0,
        **changes,
    }


def test_fixed_pose_residuals_use_normalized_image_coordinates_and_measured_ground_points():
    body = CameraSetup.model_validate(measured_body())
    result = validate_measurements(body)
    assert result["passed"]
    assert result["rmse_m"] < 0.001
    assert result["checkpoint_count"] == 3
    assert all(point["passed"] for point in result["checkpoints"])
    assert "no independent survey verification" in result["limitations"][0]
    smaller = body.model_copy(
        update={"pose": body.pose.model_copy(update={"frame_width": 640, "frame_height": 360})}
    )
    assert validate_measurements(smaller)["rmse_m"] == result["rmse_m"]
    body.checkpoints[0].measured_east_m += 4
    failed = validate_measurements(body)
    assert not failed["passed"] and failed["max_error_m"] > 3.9
    assert "exceeds" in failed["problems"][0]


def test_validation_cannot_claim_readiness_without_independent_coverage_or_measurement_note():
    empty = validate_measurements(
        CameraSetup.model_validate(measured_body(checkpoints=[], measurement_note=""))
    )
    assert not empty["passed"] and empty["rmse_m"] is None
    assert len(empty["problems"]) == 2
    body = measured_body()
    for point in body["checkpoints"]:
        point["image_y"] = 0.5
        point["measured_north_m"] = 5
    result = validate_measurements(CameraSetup.model_validate(body))
    assert any("collinear" in problem for problem in result["problems"])
    assert any("cover an area" in problem for problem in result["problems"])


@pytest.mark.parametrize(
    "pose", [{"tilt_deg": 1, "vfov_deg": 80}, {"tilt_deg": 89, "vfov_deg": 80}, {"range_m": 1}]
)
def test_out_of_model_rays_cannot_pass_or_produce_negative_ground_ranges(pose):
    body = measured_body()
    body["pose"].update(pose)
    result = validate_measurements(CameraSetup.model_validate(body))
    assert not result["passed"]
    assert any(point["error_m"] is None for point in result["checkpoints"])


@pytest.mark.parametrize(
    "field,value",
    [("frame_width", 0), ("height_m", -1), ("lat", float("nan")), ("range_m", float("inf"))],
)
def test_invalid_or_missing_measurements_are_not_silently_defaulted(field, value):
    body = measured_body()
    body["pose"][field] = value
    with pytest.raises(ValidationError):
        CameraSetup.model_validate(body)
    body = measured_body()
    body["checkpoints"][0]["image_x"] = 1.1
    with pytest.raises(ValidationError):
        CameraSetup.model_validate(body)


@pytest.fixture
async def camera_client(tmp_path, monkeypatch):
    store = Store()
    await store.put(
        "tenant-a",
        "site",
        "site-a",
        {
            "site_id": "site-a",
            "name": "Survey site",
            "cameras": [{"camera_id": "camera-a", "name": "Gate", "source_id": "rtsp-a"}],
        },
    )
    sources = [
        {
            "source_id": "rtsp-a",
            "kind": "camera_rtsp",
            "label": "Camera",
            "status": "idle",
            "restart_required": True,
        }
    ]

    async def rtsp(settings, request):
        return deepcopy(sources) if current_tenant() == "tenant-a" else []

    monkeypatch.setattr(module, "rtsp_sources", rtsp)

    class Pool:
        def __init__(self):
            self.rows = []
            self.calls = []

        async def fetch(self, sql, params):
            self.calls.append((sql, params))
            return deepcopy(self.rows)

    pool = Pool()
    settings = Settings(_env_file=None, data_dir=tmp_path, auth_mode="dev", auth_required=True)
    issuer = DevJwtAuth(settings)
    app = FastAPI()
    install_camera_commissioning_routes(app, settings, store, pool)
    install_governance(app, service="api", settings=settings, authenticator=issuer)

    @app.exception_handler(WorkbenchConflict)
    async def conflicts(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    def headers(tenant="tenant-a", role="integrator", subject="alice", zones=()):
        return {
            "Authorization": "Bearer "
            + issuer.issue(
                tenant_id=tenant, roles=(role,), subject=subject, zones=zones, clearance=3
            )
        }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, store, pool, headers, sources


@pytest.mark.asyncio
async def test_camera_setup_authorship_cas_tenant_scope_and_validation_never_edit_source_or_site(
    camera_client,
):
    client, store, _, headers, sources = camera_client
    before = deepcopy(store.rows)
    assert (await client.get("/api/camera-setups")).status_code == 401
    assert (
        await client.post(
            "/api/camera-setups", headers=headers(role="viewer"), json=measured_body()
        )
    ).status_code == 403
    response = await client.post("/api/camera-setups", headers=headers(), json=measured_body())
    assert response.status_code == 200, response.text
    row = response.json()
    assert row["status"] == "draft" and row["updated_by"] == "alice"
    path = "/api/camera-setups/" + row["setup_id"]
    foreign = headers(tenant="tenant-b")
    assert (await client.get(path, headers=foreign)).status_code == 404
    assert (await client.get("/api/camera-setups", headers=foreign)).json()["setups"] == []
    validated = await client.post(
        path + "/validate", headers=headers(subject="surveyor"), json={"expected_revision": 1}
    )
    assert validated.status_code == 200, validated.text
    assert validated.json()["status"] == "validated"
    assert validated.json()["validation"]["validated_by"] == "surveyor"
    assert validated.json()["activation"] == "not_applied"
    assert (
        await client.post(path + "/validate", headers=headers(), json={"expected_revision": 1})
    ).status_code == 409
    changed = await client.put(
        path, headers=headers(), json=measured_body(title="Revised pose", expected_revision=2)
    )
    assert changed.status_code == 200
    assert changed.json()["status"] == "draft" and changed.json()["validation"] is None
    assert store.rows[("tenant-a", "site", "site-a")] == before[("tenant-a", "site", "site-a")]
    assert sources == [
        {
            "source_id": "rtsp-a",
            "kind": "camera_rtsp",
            "label": "Camera",
            "status": "idle",
            "restart_required": True,
        }
    ]
    assert (
        await client.post(
            "/api/camera-setups", headers=headers(), json=measured_body(updated_by="forged")
        )
    ).status_code == 422


@pytest.mark.asyncio
async def test_camera_setup_requires_real_scoped_source_site_and_camera(camera_client):
    client, _, _, headers, _ = camera_client
    for changes in [
        {"source_id": "simulator"},
        {"site_id": "foreign-site"},
        {"camera_id": "absent-camera"},
    ]:
        response = await client.post(
            "/api/camera-setups", headers=headers(), json=measured_body(**changes)
        )
        assert response.status_code == 404
    assert (
        await client.get("/api/camera-setups/context", headers=headers(tenant="tenant-b"))
    ).json()["sources"] == []


@pytest.mark.asyncio
async def test_preview_only_returns_recent_indexed_sanitized_same_tenant_keys(camera_client):
    client, _, pool, headers, _ = camera_client
    path = "/api/camera-setups/sources/rtsp-a/preview"
    result = await client.get(path, headers=headers())
    assert result.status_code == 200 and not result.json()["available"]
    assert pool.calls[-1][1] == ("tenant-a", "rtsp-a")
    assert "redacted = true" in pool.calls[-1][0] and "5 minutes" in pool.calls[-1][0]
    row = {
        "frame_id": "frame",
        "source_id": "rtsp-a",
        "object_key": "tenants/tenant-a/frames/frame.jpg",
        "redacted": True,
        "ts": "2026-09-11T08:00:00Z",
        "width": 640,
        "height": 360,
    }
    for unsafe in [
        "tenants/tenant-b/frames/frame.jpg",
        "pending/frame.jpg",
        "https://example.com/frame.jpg",
        "../../frame.jpg",
    ]:
        pool.rows = [{**row, "object_key": unsafe}]
        assert not (await client.get(path, headers=headers())).json()["available"]
    pool.rows = [row]
    good = (await client.get(path, headers=headers())).json()
    assert good["available"] and good["media_url"] == "/media/tenants/tenant-a/frames/frame.jpg"
    assert not (await client.get(path, headers=headers(zones=("one-zone",)))).json()["available"]
    assert (await client.get(path, headers=headers(tenant="tenant-b"))).status_code == 404


@pytest.mark.asyncio
async def test_source_inventory_forwards_caller_identity_and_removes_connection_configuration(
    monkeypatch, tmp_path
):
    calls = []

    class Upstream:
        def __init__(self, **options):
            assert options == {"timeout": 4, "trust_env": False}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, **options):
            calls.append((url, options))
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={
                    "sources": [
                        {
                            "source_id": "camera",
                            "kind": "camera_rtsp",
                            "options": {"url": "rtsp://secret:password@camera"},
                            "last_observation": {"private": "not needed"},
                        },
                        {"source_id": "demo", "kind": "simulator"},
                    ]
                },
            )

    monkeypatch.setattr(module.httpx, "AsyncClient", Upstream)
    request = Request(
        {"type": "http", "headers": [(b"authorization", b"Bearer caller-tenant-token")]}
    )
    settings = Settings(_env_file=None, data_dir=tmp_path, ingest_port=15801)
    result = await module.rtsp_sources(settings, request)
    assert calls == [
        (
            "http://127.0.0.1:15801/sources",
            {"headers": {"Authorization": "Bearer caller-tenant-token"}},
        )
    ]
    assert [row["source_id"] for row in result] == ["camera"]
    assert "password" not in str(result) and "options" not in result[0]
    assert "last_observation" not in result[0]


@pytest.mark.asyncio
async def test_unavailable_camera_service_preserves_site_context_with_explicit_missing_state(
    camera_client, monkeypatch
):
    client, _, _, headers, _ = camera_client

    async def unavailable(settings, request):
        raise HTTPException(503, "Camera sources are unavailable")

    monkeypatch.setattr(module, "rtsp_sources", unavailable)
    response = await client.get("/api/camera-setups/context", headers=headers())
    assert response.status_code == 200
    assert not response.json()["sources_available"]
    assert response.json()["source_error"] == "Camera sources are unavailable"
    assert response.json()["sites"][0]["site_id"] == "site-a"
