"""Versioned site layouts. Image coordinates are deliberately separate from surveyed poses."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from sio_core.guard import principal_of
from sio_core.tenancy import current_tenant

from .video_rules import ZoneInput


class CameraPose(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    bearing_deg: float = Field(ge=0, lt=360)
    height_m: float = Field(gt=0, le=500)
    tilt_deg: float = Field(gt=0, lt=90)
    fov_deg: float = Field(gt=0, lt=180)
    vfov_deg: float = Field(gt=0, lt=180)
    frame_width: int = Field(ge=16, le=16384)
    frame_height: int = Field(ge=16, le=16384)


class SiteCamera(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    camera_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    name: str = Field(min_length=1, max_length=120)
    source_id: str = Field(default="", max_length=120)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    pose: CameraPose | None = None


class SiteEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    notes: str = Field(default="", max_length=4000)
    zones: list[ZoneInput] = Field(default_factory=list, max_length=30)
    cameras: list[SiteCamera] = Field(default_factory=list, max_length=50)
    expected_revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def distinct_ids(self):
        if len({zone.zone_id for zone in self.zones}) != len(self.zones):
            raise ValueError("Each zone needs a unique identifier")
        if len({camera.camera_id for camera in self.cameras}) != len(self.cameras):
            raise ValueError("Each camera needs a unique identifier")
        if not self.name.strip():
            raise ValueError("Enter a site name")
        return self


def sanitize_plan(data: bytes) -> bytes:
    """Decode a bounded raster and strip metadata before saving it as PNG."""
    from io import BytesIO

    import cv2
    import numpy as np

    # Read dimensions before OpenCV allocates the decoded pixel buffer.
    from PIL import Image

    try:
        with Image.open(BytesIO(data)) as header:
            width, height = header.size
            if header.format not in {"PNG", "JPEG"} or not (
                32 <= width <= 4096 and 32 <= height <= 4096 and width * height <= 12_000_000
            ):
                raise ValueError(
                    "Choose a PNG or JPEG between 32 and 4096 pixels, up to 12 megapixels"
                )
        decoded = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if decoded is None:
            raise ValueError("The floorplan could not be decoded")
        ok, encoded = cv2.imencode(".png", decoded)
        if not ok:
            raise ValueError("The floorplan could not be converted")
        return encoded.tobytes()
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("Choose a valid PNG or JPEG floorplan") from error


def install_site_routes(app: FastAPI, settings: Any, store: Any) -> None:
    root = (Path(settings.data_dir) / "site_plans").resolve()

    async def get_site(site_id: str) -> dict:
        value = await store.get(current_tenant(), "site", site_id)
        if value is None:
            raise HTTPException(404, "Site not found")
        return value

    @app.get("/api/sites", tags=["sites"])
    async def sites():
        return {"sites": await store.list(current_tenant(), "site")}

    @app.get("/api/sites/{site_id}", tags=["sites"])
    async def site(site_id: str):
        return await get_site(site_id)

    async def save(body: SiteEdit, request: Request, old: dict | None = None):
        if old is None and body.expected_revision != 0:
            raise HTTPException(422, "New sites start at revision zero")
        if old is not None and body.expected_revision < 1:
            raise HTTPException(422, "Reload the site revision before saving")
        site_id = old["site_id"] if old else "site_" + uuid.uuid4().hex
        return await store.put(
            current_tenant(),
            "site",
            site_id,
            {
                **(old or {}),
                **body.model_dump(exclude={"expected_revision"}),
                "site_id": site_id,
                "updated_by": principal_of(request).subject,
                "coordinates": "normalized_image",
                "calibration_status": "draft",
                "calibration_note": "Saved poses are planning records. Survey and commission the source calibration before using geographic detections.",
            },
            expected_revision=body.expected_revision,
        )

    @app.post("/api/sites", tags=["sites"])
    async def create(body: SiteEdit, request: Request):
        if len(await store.list(current_tenant(), "site", limit=100)) >= 100:
            raise HTTPException(409, "This workspace is limited to 100 sites")
        return await save(body, request)

    @app.put("/api/sites/{site_id}", tags=["sites"])
    async def update(site_id: str, body: SiteEdit, request: Request):
        return await save(body, request, await get_site(site_id))

    @app.post("/api/sites/{site_id}/floorplan", tags=["sites"])
    async def upload(site_id: str, request: Request):
        old = await get_site(site_id)
        try:
            revision = int(request.headers.get("x-revision", "0"))
        except ValueError:
            raise HTTPException(422, "Supply the current X-Revision") from None
        if revision < 1:
            raise HTTPException(422, "Supply the current X-Revision")
        if request.headers.get("content-type", "").split(";")[0] not in {"image/png", "image/jpeg"}:
            raise HTTPException(415, "Choose a PNG or JPEG floorplan")
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 8 * 1024 * 1024:
                raise HTTPException(413, "Floorplan exceeds 8 MiB")
        try:
            png = await asyncio.to_thread(sanitize_plan, bytes(data))
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        tenant_key = hashlib.sha256(current_tenant().encode()).hexdigest()
        directory = root / tenant_key
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        filename = uuid.uuid4().hex + ".png"
        path = directory / filename
        await asyncio.to_thread(path.write_bytes, png)
        path.chmod(0o600)
        try:
            return await store.put(
                current_tenant(),
                "site",
                site_id,
                {
                    **old,
                    "floorplan_file": filename,
                    "floorplan_url": f"/api/sites/{site_id}/floorplan?version={filename}",
                    "updated_by": principal_of(request).subject,
                },
                expected_revision=revision,
            )
        except Exception:
            path.unlink(missing_ok=True)
            raise

    @app.get("/api/sites/{site_id}/floorplan", tags=["sites"])
    async def floorplan(site_id: str):
        row = await get_site(site_id)
        filename = row.get("floorplan_file", "")
        tenant_key = hashlib.sha256(current_tenant().encode()).hexdigest()
        directory = root / tenant_key
        path = (directory / filename).resolve()
        if not filename or path.parent != directory or not path.is_file():
            raise HTTPException(404, "Floorplan unavailable")
        return FileResponse(
            path,
            media_type="image/png",
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )
