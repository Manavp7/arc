"""Measured camera readiness records. Validation never activates or edits an RTSP source."""

from __future__ import annotations

import math
from copy import deepcopy
from itertools import combinations
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import ConfigDict, Field, model_validator
from sio_fusion.projection import CameraCalibration, GroundProjector, to_local_metres

from sio_core.guard import bearer_token
from sio_core.tenancy import current_tenant
from sio_schemas import BBox, new_id

from .case_evidence import safe_frame_key
from .case_helpers import StrictBody, iso_now
from .cases import actor, need
from .sites import CameraPose


class MeasuredPose(CameraPose):
    range_m: float = Field(default=60, gt=0, le=10000)


class Checkpoint(StrictBody):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)
    checkpoint_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    label: str = Field(min_length=1, max_length=100)
    image_x: float = Field(ge=0, le=1)
    image_y: float = Field(ge=0, le=1)
    measured_east_m: float = Field(ge=-10000, le=10000)
    measured_north_m: float = Field(ge=-10000, le=10000)


class CameraSetup(StrictBody):
    title: str = Field(min_length=1, max_length=160)
    source_id: str = Field(min_length=1, max_length=120)
    site_id: str = Field(min_length=1, max_length=120)
    camera_id: str = Field(min_length=1, max_length=120)
    pose: MeasuredPose
    checkpoints: list[Checkpoint] = Field(default_factory=list, max_length=20)
    tolerance_m: float = Field(default=2, ge=0.1, le=20, allow_inf_nan=False)
    measurement_note: str = Field(default="", max_length=4000)
    expected_revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def distinct_checkpoints(self):
        if len({point.checkpoint_id for point in self.checkpoints}) != len(self.checkpoints):
            raise ValueError("Checkpoint identifiers must be unique")
        return self


class ValidateSetup(StrictBody):
    expected_revision: int = Field(ge=1)


def validate_measurements(body: CameraSetup) -> dict[str, Any]:
    """Compare a fixed measured camera pose to independently supplied survey checkpoints."""
    problems = []
    if len(body.checkpoints) < 3:
        problems.append("Add at least three independently measured checkpoints")
    if not body.measurement_note:
        problems.append(
            "Record how and when the checkpoint distances and camera pose were measured"
        )
    coords = [(point.image_x, point.image_y) for point in body.checkpoints]
    if len(coords) >= 3 and not any(
        abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) > 0.001
        for a, b, c in combinations(coords, 3)
    ):
        problems.append(
            "Spread checkpoints across the image; collinear points do not check the full view"
        )
    measured = [(point.measured_east_m, point.measured_north_m) for point in body.checkpoints]
    if len(measured) >= 3 and not any(
        abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) > 0.01
        for a, b, c in combinations(measured, 3)
    ):
        problems.append("Measured ground checkpoints must cover an area")
    pose = body.pose
    calibration = CameraCalibration.from_source_row(
        {"source_id": body.source_id, "lat": pose.lat, "lon": pose.lon, "config": pose.model_dump()}
    )
    if calibration is None:
        raise HTTPException(422, "Camera pose cannot produce a ground projection")
    projector = GroundProjector(calibration)
    rows = []
    for point in body.checkpoints:
        x, y = point.image_x * pose.frame_width, point.image_y * pose.frame_height
        depression = pose.tilt_deg + (point.image_y - 0.5) * pose.vfov_deg
        fix = projector.project(BBox(x1=x, y1=y, x2=x, y2=y)) if 0.15 < depression < 90 else None
        if fix is not None and fix.range_m > pose.range_m:
            fix = None
        row: dict[str, Any] = point.model_dump()
        if fix is None:
            row.update(error_m=None, projected_east_m=None, projected_north_m=None, passed=False)
            problems.append(
                f"{point.label}: point is outside the flat-ground view or configured range"
            )
        else:
            east, north = to_local_metres(fix.geo, calibration.geo)
            error = math.hypot(east - point.measured_east_m, north - point.measured_north_m)
            row.update(
                error_m=round(error, 4),
                projected_east_m=round(east, 4),
                projected_north_m=round(north, 4),
                passed=error <= body.tolerance_m,
            )
            if error > body.tolerance_m:
                problems.append(
                    f"{point.label}: residual {error:.2f} m exceeds {body.tolerance_m:.2f} m tolerance"
                )
        rows.append(row)
    errors = [row["error_m"] for row in rows if row["error_m"] is not None]
    return {
        "passed": not problems,
        "checkpoint_count": len(rows),
        "checkpoints": rows,
        "rmse_m": round(math.sqrt(sum(error**2 for error in errors) / len(errors)), 4)
        if errors
        else None,
        "max_error_m": max(errors) if errors else None,
        "tolerance_m": body.tolerance_m,
        "problems": problems,
        "validated_at": iso_now(),
        "method": "fixed_pose_flat_ground_checkpoint_residuals",
        "limitations": [
            "Results compare the entered pose with user-supplied surveyed points; no independent survey verification was performed.",
            "The flat-ground camera model does not validate terrain slope, lens distortion or live detection accuracy.",
            "Passing these checks records readiness only. Source calibration and live operation remain unchanged.",
        ],
    }


async def rtsp_sources(settings: Any, request: Request) -> list[dict[str, Any]]:
    token = bearer_token(request)
    if not token:
        raise HTTPException(401, "Sign in to inspect camera sources")
    try:
        async with httpx.AsyncClient(timeout=4, trust_env=False) as client:
            response = await client.get(
                f"http://127.0.0.1:{settings.ingest_port}/sources",
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code in (401, 403):
            raise HTTPException(response.status_code, "Camera source access was refused")
        response.raise_for_status()
        rows = response.json()["sources"]
        fields = (
            "source_id",
            "kind",
            "label",
            "status",
            "enabled",
            "active",
            "rate_hz",
            "restart_required",
            "last_success",
            "error",
            "last_test",
        )
        return [
            {key: deepcopy(row.get(key)) for key in fields}
            for row in rows
            if row.get("kind") == "camera_rtsp"
        ]
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            503, "Camera sources are unavailable. Check ingestion health and retry."
        ) from error


def install_camera_commissioning_routes(app: FastAPI, settings: Any, store: Any, pool: Any) -> None:
    async def linked(body: CameraSetup, request: Request):
        source = next(
            (
                row
                for row in await rtsp_sources(settings, request)
                if row["source_id"] == body.source_id
            ),
            None,
        )
        if source is None:
            raise HTTPException(404, "Select an existing RTSP camera source")
        site = await store.get(current_tenant(), "site", body.site_id)
        if site is None:
            raise HTTPException(404, "Site not found")
        camera = next(
            (row for row in site.get("cameras", []) if row.get("camera_id") == body.camera_id), None
        )
        if camera is None:
            raise HTTPException(404, "Place a camera on the selected site before linking its setup")
        return source, site, camera

    async def load(setup_id: str):
        row = await store.get(current_tenant(), "camera_setup", setup_id)
        if row is None:
            raise HTTPException(404, "Camera setup not found")
        return row

    @app.get("/api/camera-setups/context", tags=["camera-setup"])
    async def context(request: Request):
        actor(request)
        sites = await store.list(current_tenant(), "site", limit=100)
        try:
            sources = await rtsp_sources(settings, request)
            return {
                "sites": sites,
                "sources": sources,
                "sources_available": True,
                "source_error": None,
            }
        except HTTPException as error:
            if error.status_code != 503:
                raise
            return {
                "sites": sites,
                "sources": [],
                "sources_available": False,
                "source_error": error.detail,
            }

    @app.get("/api/camera-setups/sources/{source_id}/preview", tags=["camera-setup"])
    async def preview(source_id: str, request: Request):
        principal = actor(request)
        need(principal, "media.read")
        if not any(row["source_id"] == source_id for row in await rtsp_sources(settings, request)):
            raise HTTPException(404, "RTSP source not found")
        if principal.zones:
            return {
                "available": False,
                "reason": "Whole-camera preview requires unrestricted zone access because indexed frames do not identify every visible zone.",
            }
        rows = await pool.fetch(
            "SELECT frame_id, object_key, source_id, ts, width, height, redacted FROM frames WHERE tenant_id = %s AND source_id = %s AND redacted = true AND ts > now() - interval '5 minutes' ORDER BY ts DESC LIMIT 1",
            (current_tenant(), source_id),
        )
        if not rows or not safe_frame_key(rows[0]["object_key"], current_tenant()):
            return {
                "available": False,
                "reason": "No recent sanitized frame is indexed for this camera. A connection test alone does not publish preview footage.",
            }
        from urllib.parse import quote

        frame = rows[0]
        return jsonable_encoder(
            {
                "available": True,
                "media_url": "/media/" + quote(frame["object_key"], safe="/"),
                **{
                    key: frame.get(key)
                    for key in ("frame_id", "source_id", "ts", "width", "height", "redacted")
                },
            }
        )

    @app.get("/api/camera-setups", tags=["camera-setup"])
    async def list_setups(request: Request):
        actor(request)
        return {"setups": await store.list(current_tenant(), "camera_setup", limit=200)}

    @app.get("/api/camera-setups/{setup_id}", tags=["camera-setup"])
    async def get_setup(setup_id: str, request: Request):
        actor(request)
        return await load(setup_id)

    async def save(body: CameraSetup, request: Request, setup_id: str | None = None):
        principal = actor(request)
        need(principal, "site.write")
        if setup_id:
            await load(setup_id)
            if body.expected_revision < 1:
                raise HTTPException(422, "Reload the current setup revision")
        elif body.expected_revision != 0:
            raise HTTPException(422, "A new setup starts at revision zero")
        source, site, camera = await linked(body, request)
        setup_id = setup_id or new_id("camera_setup")
        record = {
            **body.model_dump(mode="json", exclude={"expected_revision"}),
            "setup_id": setup_id,
            "status": "draft",
            "source_snapshot": source,
            "site_snapshot": {key: site.get(key) for key in ("site_id", "name", "revision")},
            "camera_snapshot": camera,
            "validation": None,
            "updated_by": principal.subject,
            "activation": "not_applied",
        }
        return await store.put(
            current_tenant(),
            "camera_setup",
            setup_id,
            record,
            expected_revision=body.expected_revision,
        )

    @app.post("/api/camera-setups", tags=["camera-setup"])
    async def create_setup(body: CameraSetup, request: Request):
        if len(await store.list(current_tenant(), "camera_setup", limit=200)) >= 200:
            raise HTTPException(409, "This workspace is limited to 200 camera setup records")
        return await save(body, request)

    @app.put("/api/camera-setups/{setup_id}", tags=["camera-setup"])
    async def update_setup(setup_id: str, body: CameraSetup, request: Request):
        return await save(body, request, setup_id)

    @app.post("/api/camera-setups/{setup_id}/validate", tags=["camera-setup"])
    async def validate_setup(setup_id: str, body: ValidateSetup, request: Request):
        principal = actor(request)
        need(principal, "site.write")
        old = await load(setup_id)
        setup = CameraSetup.model_validate(
            {key: old[key] for key in CameraSetup.model_fields if key in old}
        )
        source, site, camera = await linked(setup, request)
        result = validate_measurements(setup)
        return await store.put(
            current_tenant(),
            "camera_setup",
            setup_id,
            {
                **old,
                "status": "validated" if result["passed"] else "draft",
                "validation": {**result, "validated_by": principal.subject},
                "source_snapshot": source,
                "site_snapshot": {key: site.get(key) for key in ("site_id", "name", "revision")},
                "camera_snapshot": camera,
                "updated_by": principal.subject,
            },
            expected_revision=body.expected_revision,
        )
