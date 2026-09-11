"""Site authoring keeps image space distinct and uploads bounded, sanitized and scoped."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from io import BytesIO

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from PIL import Image, PngImagePlugin
from pydantic import ValidationError
from sio_api.sites import CameraPose, SiteEdit, install_site_routes, sanitize_plan
from sio_api.workbench_store import WorkbenchConflict

from sio_core.authn import DevJwtAuth
from sio_core.config import Settings
from sio_core.guard import install_governance


class SiteStore:
    def __init__(self):
        self.rows = {}

    async def list(self, tenant, kind, limit=500):
        return [
            deepcopy(value)
            for (row_tenant, row_kind, _), value in self.rows.items()
            if row_tenant == tenant and row_kind == kind
        ][:limit]

    async def get(self, tenant, kind, record_id):
        return deepcopy(self.rows.get((tenant, kind, record_id)))

    async def put(self, tenant, kind, record_id, data, expected_revision=None):
        key = tenant, kind, record_id
        old = self.rows.get(key)
        revision = old["revision"] if old else 0
        if revision != expected_revision:
            raise WorkbenchConflict("Refresh the current revision")
        self.rows[key] = {**deepcopy(data), "record_id": record_id, "revision": revision + 1}
        return deepcopy(self.rows[key])


def image_bytes(size=(128, 96), *, format="PNG", metadata=False):
    output = BytesIO()
    info = PngImagePlugin.PngInfo()
    if metadata:
        info.add_text("private-comment", "private floorplan notes")
    Image.new("RGB", size, "#254836").save(output, format=format, pnginfo=info)
    return output.getvalue()


def edit_body(**overrides):
    return {
        "name": "Depot",
        "notes": "Planning layout",
        "zones": [
            {
                "zone_id": "loading",
                "name": "Loading area",
                "points": [[0.1, 0.1], [0.8, 0.1], [0.8, 0.8], [0.1, 0.8]],
            }
        ],
        "cameras": [
            {
                "camera_id": "camera-a",
                "name": "Gate camera",
                "source_id": "camera_rtsp-a",
                "x": 0.2,
                "y": 0.3,
            }
        ],
        **overrides,
    }


@pytest.fixture
async def sites_client(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, auth_mode="dev", auth_required=True)
    app = FastAPI()
    store = SiteStore()
    issuer = DevJwtAuth(settings)
    install_site_routes(app, settings, store)
    install_governance(app, service="api", settings=settings, authenticator=issuer)

    @app.exception_handler(WorkbenchConflict)
    async def conflicts(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:

        def headers(*, tenant="tenant-a", role="integrator", subject="alice"):
            return {
                "Authorization": "Bearer "
                + issuer.issue(tenant_id=tenant, roles=(role,), subject=subject, clearance=3)
            }

        yield client, store, headers, tmp_path


@pytest.mark.asyncio
async def test_site_routes_require_auth_and_site_write_role(sites_client):
    client, _, headers, _ = sites_client
    assert (await client.get("/api/sites")).status_code == 401
    viewer = headers(role="viewer")
    assert (await client.get("/api/sites", headers=viewer)).status_code == 200
    assert (await client.post("/api/sites", headers=viewer, json=edit_body())).status_code == 403
    operator = headers(role="operator")
    assert (await client.post("/api/sites", headers=operator, json=edit_body())).status_code == 403
    result = await client.post("/api/sites", headers=headers(), json=edit_body())
    assert result.status_code == 200
    assert result.json()["updated_by"] == "alice"
    assert result.json()["coordinates"] == "normalized_image"
    assert result.json()["calibration_status"] == "draft"
    assert "commission" in result.json()["calibration_note"]


@pytest.mark.asyncio
async def test_site_tenant_isolation_revision_conflict_and_authored_update(sites_client):
    client, _, headers, _ = sites_client
    created = (await client.post("/api/sites", headers=headers(), json=edit_body())).json()
    path = "/api/sites/" + created["site_id"]
    updated = await client.put(
        path,
        headers=headers(subject="bob"),
        json=edit_body(name="Depot revised", expected_revision=1),
    )
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    assert updated.json()["updated_by"] == "bob"
    stale = await client.put(
        path, headers=headers(), json=edit_body(name="Stale", expected_revision=1)
    )
    assert stale.status_code == 409
    no_revision = await client.put(path, headers=headers(), json=edit_body())
    assert no_revision.status_code == 422
    foreign = headers(tenant="tenant-b")
    assert (await client.get(path, headers=foreign)).status_code == 404
    assert (await client.get("/api/sites", headers=foreign)).json()["sites"] == []
    spoof = await client.post(
        "/api/sites", headers=headers(), json=edit_body(updated_by="commander")
    )
    assert spoof.status_code == 422


@pytest.mark.asyncio
async def test_floorplan_is_sanitized_tenant_scoped_and_preserved_on_stale_upload(sites_client):
    client, _, headers, root = sites_client
    auth = headers()
    site = (await client.post("/api/sites", headers=auth, json=edit_body())).json()
    url = f"/api/sites/{site['site_id']}/floorplan"
    upload = await client.post(
        url,
        headers={**auth, "Content-Type": "image/png", "X-Revision": "1"},
        content=image_bytes(metadata=True),
    )
    assert upload.status_code == 200
    assert upload.json()["revision"] == 2
    filename = upload.json()["floorplan_file"]
    folder = root / "site_plans" / hashlib.sha256(b"tenant-a").hexdigest()
    assert (folder / filename).stat().st_mode & 0o777 == 0o600
    fetched = await client.get(upload.json()["floorplan_url"], headers=headers(role="viewer"))
    assert fetched.status_code == 200
    assert fetched.headers["cache-control"] == "private, no-store"
    with Image.open(BytesIO(fetched.content)) as image:
        assert image.size == (128, 96)
        assert "private-comment" not in image.info
    stale = await client.post(
        url,
        headers={**auth, "Content-Type": "image/png", "X-Revision": "1"},
        content=image_bytes((64, 64)),
    )
    assert stale.status_code == 409
    assert [path.name for path in folder.iterdir()] == [filename]
    assert (await client.get(url, headers=headers(tenant="tenant-b"))).status_code == 404
    assert (
        await client.post(
            url,
            headers={**headers(role="viewer"), "Content-Type": "image/png", "X-Revision": "2"},
            content=image_bytes(),
        )
    ).status_code == 403


@pytest.mark.asyncio
async def test_floorplan_rejects_missing_revision_unsupported_format_invalid_bytes_and_large_body(
    sites_client,
):
    client, _, headers, root = sites_client
    auth = headers()
    site = (await client.post("/api/sites", headers=auth, json=edit_body())).json()
    url = f"/api/sites/{site['site_id']}/floorplan"
    for extra, content, status in [
        ({"Content-Type": "image/png"}, image_bytes(), 422),
        ({"Content-Type": "image/png", "X-Revision": "oops"}, image_bytes(), 422),
        ({"Content-Type": "image/svg+xml", "X-Revision": "1"}, b"<svg/>", 415),
        ({"Content-Type": "image/png", "X-Revision": "1"}, b"not an image", 422),
        ({"Content-Type": "image/png", "X-Revision": "1"}, image_bytes((16, 16)), 422),
        ({"Content-Type": "image/png", "X-Revision": "1"}, b"x" * (8 * 1024 * 1024 + 1), 413),
    ]:
        result = await client.post(url, headers={**auth, **extra}, content=content)
        assert result.status_code == status, result.text
    assert list(root.rglob("*.png")) == []


@pytest.mark.asyncio
async def test_floorplan_reference_cannot_escape_tenant_directory(sites_client, tmp_path):
    client, store, headers, _root = sites_client
    site = (await client.post("/api/sites", headers=headers(), json=edit_body())).json()
    outside = tmp_path / "outside.png"
    outside.write_bytes(image_bytes())
    store.rows[("tenant-a", "site", site["site_id"])]["floorplan_file"] = str(outside)
    response = await client.get(f"/api/sites/{site['site_id']}/floorplan", headers=headers())
    assert response.status_code == 404


def test_site_models_reject_duplicate_geometry_invalid_coordinates_and_unsurveyed_pose():
    with pytest.raises(ValidationError):
        SiteEdit.model_validate(edit_body(name="  "))
    with pytest.raises(ValidationError):
        SiteEdit.model_validate(edit_body(zones=edit_body()["zones"] * 2))
    with pytest.raises(ValidationError):
        SiteEdit.model_validate(edit_body(cameras=edit_body()["cameras"] * 2))
    with pytest.raises(ValidationError):
        SiteEdit.model_validate(edit_body(cameras=[{**edit_body()["cameras"][0], "x": 1.1}]))
    with pytest.raises(ValidationError):
        SiteEdit.model_validate(
            edit_body(
                zones=[{"zone_id": "z", "name": "z", "points": [[0, 0], [1, 1], [0, 1], [1, 0]]}]
            )
        )
    pose = {
        "lat": 37.77,
        "lon": -122.41,
        "bearing_deg": 90,
        "height_m": 5,
        "tilt_deg": 20,
        "fov_deg": 70,
        "vfov_deg": 45,
        "frame_width": 1920,
        "frame_height": 1080,
    }
    assert CameraPose.model_validate(pose).frame_width == 1920
    for override in (
        {"frame_width": 0},
        {"frame_height": -1},
        {"tilt_deg": 0},
        {"lat": float("nan")},
        {"height_m": float("inf")},
    ):
        with pytest.raises(ValidationError):
            CameraPose.model_validate({**pose, **override})


def test_floorplan_sanitizer_refuses_large_decoded_pixels_and_non_raster():
    with pytest.raises(ValueError):
        sanitize_plan(image_bytes((4096, 4096)))
    with pytest.raises(ValueError):
        sanitize_plan(image_bytes(format="GIF"))
    with Image.open(BytesIO(sanitize_plan(image_bytes(format="JPEG")))) as image:
        assert image.format == "PNG"
        assert image.size == (128, 96)


def test_site_concave_polygon_allows_collinear_disjoint_edges():
    site = SiteEdit.model_validate(
        edit_body(
            zones=[
                {
                    "zone_id": "u",
                    "name": "U-shaped loading area",
                    "points": [
                        [0, 0],
                        [1, 0],
                        [1, 1],
                        [0.7, 1],
                        [0.7, 0.7],
                        [0.3, 0.7],
                        [0.3, 1],
                        [0, 1],
                    ],
                }
            ]
        )
    )
    assert len(site.zones[0].points) == 8
