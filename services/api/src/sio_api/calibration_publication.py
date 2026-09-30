"""Review a saved measured pose, atomically publish it, and observe fusion acknowledgement."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request
from pydantic import Field

from sio_core import PgPool
from sio_core.tenancy import current_tenant
from sio_schemas import new_id

from .case_helpers import StrictBody
from .cases import actor, need

POSE_KEYS = (
    "bearing_deg",
    "fov_deg",
    "range_m",
    "height_m",
    "tilt_deg",
    "vfov_deg",
    "frame_width",
    "frame_height",
)
NOTE = "Publishing updates the flat-ground fusion calibration. It does not activate a camera or independently verify survey or detection accuracy."
SOURCE_SQL = "SELECT source_id, kind, config, calibration_revision, ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon FROM sources WHERE tenant_id = %s AND source_id = %s"


class PreviewCalibration(StrictBody):
    expected_revision: int = Field(ge=1)


class ApplyCalibration(PreviewCalibration):
    preview_id: str = Field(min_length=1, max_length=120)


def source_digest(source):
    return hashlib.sha256(json.dumps(source, sort_keys=True, default=str).encode()).hexdigest()


def pose_of(source):
    source = source or {}
    config = source.get("config") or {}
    return {
        "lat": source.get("lat"),
        "lon": source.get("lon"),
        **{key: config[key] for key in POSE_KEYS if key in config},
    }


class CalibrationPublication:
    def __init__(self, store, pool):
        self.store, self.pool = store, pool

    async def setup(self, setup_id, principal, *, write=False):
        if (
            principal.tenant_id != current_tenant()
            or not principal.subject
            or principal.subject == "anonymous"
        ):
            raise HTTPException(401, "Sign in to review calibration")
        need(principal, "site.read")
        if write:
            need(principal, "site.write")
            need(principal, "integration.write")
        # A complete physical camera pose affects every visible zone.
        if principal.zones and not principal.is_admin:
            raise HTTPException(
                403,
                "Publishing or viewing whole-camera calibration requires unrestricted zone access",
            )
        setup = await self.store.get(current_tenant(), "camera_setup", setup_id)
        if not setup:
            raise HTTPException(404, "Camera setup not found")
        return setup

    async def latest(self, tenant, setup_id):
        return await self.pool.fetchrow(
            "SELECT * FROM camera_calibration_changes WHERE tenant_id = %s AND setup_id = %s ORDER BY applied_at DESC, publication_id DESC LIMIT 1",
            (tenant, setup_id),
        )

    async def preview(self, setup_id, body, principal, *, rollback=False):
        setup = await self.setup(setup_id, principal, write=True)
        if setup["revision"] != body.expected_revision:
            raise HTTPException(409, "Camera setup changed; reload and review it again")
        tenant = current_tenant()
        source = await self.pool.fetchrow(SOURCE_SQL, (tenant, setup["source_id"]))
        if source and source["kind"] != "camera":
            raise HTTPException(409, "The registered source is not a camera")
        previous = pose_of(source)
        if rollback:
            latest = await self.latest(tenant, setup_id)
            if (
                not latest
                or latest["source_id"] != setup["source_id"]
                or latest["operation"] != "apply"
                or not source
                or source["calibration_revision"] != latest["calibration_revision"]
            ):
                raise HTTPException(409, "This publication is no longer the current calibration")
            proposed = latest["previous_pose"]
        else:
            from .camera_commissioning import CameraSetup, validate_measurements

            if setup.get("status") != "validated" or not (setup.get("validation") or {}).get(
                "passed"
            ):
                raise HTTPException(409, "Save and validate camera measurements before publishing")
            checked = CameraSetup.model_validate(
                {key: setup[key] for key in CameraSetup.model_fields if key in setup}
            )
            if not validate_measurements(checked)["passed"]:
                raise HTTPException(409, "Saved measurements no longer pass calibration validation")
            proposed = {
                "lat": setup["pose"]["lat"],
                "lon": setup["pose"]["lon"],
                **{key: setup["pose"][key] for key in POSE_KEYS},
            }
        preview_id = new_id("cal_preview")
        record = {
            "preview_id": preview_id,
            "operation": "rollback" if rollback else "apply",
            "setup_id": setup_id,
            "setup_revision": setup["revision"],
            "source_id": setup["source_id"],
            "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            "previous_pose": previous,
            "proposed_pose": proposed,
            "actor": principal.subject,
            "source_digest": source_digest(source),
            "note": NOTE,
        }
        await self.store.put(
            tenant, "camera_calibration_preview", preview_id, record, expected_revision=0
        )
        return {
            key: value for key, value in record.items() if key not in {"actor", "source_digest"}
        }

    async def apply(self, setup_id, body, principal, *, rollback=False):
        await self.setup(setup_id, principal, write=True)
        tenant = current_tenant()
        async with (
            await self.pool._conn() as conn,
            conn.transaction(),
            PgPool.dict_cursor(conn) as cur,
        ):
            await cur.execute(
                "SELECT * FROM workbench_documents WHERE tenant_id=%s AND kind='camera_calibration_preview' AND record_id=%s FOR UPDATE",
                (tenant, body.preview_id),
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(404, "Calibration preview not found")
            preview = row["payload"]
            if preview["setup_id"] != setup_id or preview["actor"] != principal.subject:
                raise HTTPException(403, "This preview belongs to another operator or setup")
            if preview.get("used") or datetime.fromisoformat(preview["expires_at"]) <= datetime.now(
                UTC
            ):
                raise HTTPException(
                    409, "Preview expired or already applied; create a fresh preview"
                )
            if preview["operation"] != ("rollback" if rollback else "apply"):
                raise HTTPException(409, "Preview operation does not match")
            await cur.execute(
                "SELECT revision FROM workbench_documents WHERE tenant_id=%s AND kind='camera_setup' AND record_id=%s FOR UPDATE",
                (tenant, setup_id),
            )
            setup_row = await cur.fetchone()
            if (
                not setup_row
                or setup_row["revision"] != body.expected_revision
                or body.expected_revision != preview["setup_revision"]
            ):
                raise HTTPException(409, "Camera setup changed; create a fresh preview")
            source_id = preview["source_id"]
            await cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (json.dumps([tenant, "calibration", source_id]),),
            )
            await cur.execute(SOURCE_SQL + " FOR UPDATE", (tenant, source_id))
            source = await cur.fetchone()
            if source_digest(source) != preview["source_digest"]:
                raise HTTPException(409, "Camera calibration changed; review a fresh preview")
            pose = preview["proposed_pose"]
            config = {
                key: value
                for key, value in ((source or {}).get("config") or {}).items()
                if key not in POSE_KEYS
            }
            config.update({key: pose[key] for key in POSE_KEYS if key in pose})
            revision = int((source or {}).get("calibration_revision", 0)) + 1
            await cur.execute(
                """INSERT INTO sources (tenant_id,source_id,kind,modality,config,geom,calibration_revision)
                VALUES (%s,%s,'camera','video',%s::jsonb,CASE WHEN %s::float8 IS NULL THEN NULL ELSE ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography END,%s)
                ON CONFLICT (tenant_id,source_id) DO UPDATE SET config=EXCLUDED.config,geom=EXCLUDED.geom,calibration_revision=EXCLUDED.calibration_revision""",
                (
                    tenant,
                    source_id,
                    json.dumps(config),
                    pose.get("lat"),
                    pose.get("lon"),
                    pose.get("lat"),
                    revision,
                ),
            )
            publication = new_id("cal")
            await cur.execute(
                """INSERT INTO camera_calibration_changes
                (tenant_id,publication_id,source_id,setup_id,setup_revision,calibration_revision,previous_pose,proposed_pose,operation,applied_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s)""",
                (
                    tenant,
                    publication,
                    source_id,
                    setup_id,
                    body.expected_revision,
                    revision,
                    json.dumps(preview["previous_pose"]),
                    json.dumps(pose),
                    preview["operation"],
                    principal.subject,
                ),
            )
            await cur.execute(
                "UPDATE workbench_documents SET payload=payload || '{\"used\": true}'::jsonb,revision=revision+1,updated_at=now() WHERE tenant_id=%s AND kind='camera_calibration_preview' AND record_id=%s",
                (tenant, body.preview_id),
            )
        return await self.status(setup_id, principal)

    async def status(self, setup_id, principal):
        setup = await self.setup(setup_id, principal)
        tenant = current_tenant()
        latest = await self.latest(tenant, setup_id)
        result = {
            "publication_id": None,
            "status": "not_published",
            "source_id": setup["source_id"],
            "calibration_revision": None,
            "applied_at": None,
            "applied_by": None,
            "fusion": {"acknowledged": False, "acknowledged_at": None},
            "can_rollback": False,
            "note": NOTE,
        }
        if not latest:
            return result
        source = await self.pool.fetchrow(SOURCE_SQL, (tenant, latest["source_id"]))
        ack = await self.pool.fetchrow(
            "SELECT * FROM camera_calibration_ack WHERE tenant_id=%s AND source_id=%s",
            (tenant, latest["source_id"]),
        )
        current = (
            source
            and setup["source_id"] == latest["source_id"]
            and source["calibration_revision"] == latest["calibration_revision"]
        )
        from sio_fusion.projection import CameraCalibration

        expected_pose = latest["proposed_pose"]
        expected_valid = (
            CameraCalibration.from_source_row(
                {
                    "source_id": latest["source_id"],
                    "lat": expected_pose.get("lat"),
                    "lon": expected_pose.get("lon"),
                    "config": {
                        key: expected_pose[key] for key in POSE_KEYS if key in expected_pose
                    },
                }
            )
            is not None
        )
        acknowledged = bool(
            current
            and ack
            and ack["calibration_revision"] == latest["calibration_revision"]
            and ack["valid"] == expected_valid
            and ack["acknowledged_at"] > datetime.now(UTC) - timedelta(seconds=30)
        )
        result.update(
            {
                key: latest[key]
                for key in (
                    "publication_id",
                    "source_id",
                    "calibration_revision",
                    "applied_by",
                    "applied_at",
                )
            }
        )
        result.update(
            status=(
                "superseded"
                if not current
                else "rolled_back"
                if acknowledged and latest["operation"] == "rollback"
                else "applied"
                if acknowledged
                else "pending_fusion"
            ),
            can_rollback=bool(current and latest["operation"] == "apply"),
            fusion={
                "acknowledged": acknowledged,
                "acknowledged_at": ack["acknowledged_at"] if ack else None,
            },
        )
        return result


def install_calibration_routes(app, store, pool):
    manager = CalibrationPublication(store, pool)
    prefix = "/api/camera-setups/{setup_id}/calibration"

    @app.post(prefix + "/preview", tags=["camera-calibration"])
    async def preview(setup_id: str, body: PreviewCalibration, request: Request):
        return await manager.preview(setup_id, body, actor(request))

    @app.post(prefix + "/apply", tags=["camera-calibration"])
    async def apply(setup_id: str, body: ApplyCalibration, request: Request):
        return await manager.apply(setup_id, body, actor(request))

    @app.get(prefix + "/status", tags=["camera-calibration"])
    async def status(setup_id: str, request: Request):
        return await manager.status(setup_id, actor(request))

    @app.post(prefix + "/rollback-preview", tags=["camera-calibration"])
    async def rollback_preview(setup_id: str, body: PreviewCalibration, request: Request):
        return await manager.preview(setup_id, body, actor(request), rollback=True)

    @app.post(prefix + "/rollback", tags=["camera-calibration"])
    async def rollback(setup_id: str, body: ApplyCalibration, request: Request):
        return await manager.apply(setup_id, body, actor(request), rollback=True)

    return manager
