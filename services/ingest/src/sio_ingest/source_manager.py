"""Local source configuration and bounded read-only connection diagnostics.

Changes are saved atomically and take effect after ingest restarts. Existing
connectors continue with their active configuration until that restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from sio_core.tenancy import current_tenant
from sio_schemas import Modality, Observation, utc_now

from .connectors.base import Connector, ConnectorConfig, build_connector, connector_kinds

SECRET = "••••••"
_SECRET_KEY = re.compile(r"password|secret|token|credential|authorization|api.?key", re.I)
_SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


def redact(value: Any, key: str = "") -> Any:
    if _SECRET_KEY.search(key):
        return SECRET if value else value
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str) and "://" in value:
        try:
            parts = urlsplit(value)
            host = parts.netloc.rsplit("@", 1)[-1]
            query = urlencode(
                [(k, SECRET if _SECRET_KEY.search(k) else v) for k, v in parse_qsl(parts.query)]
            )
            return urlunsplit((parts.scheme, host, parts.path, query, ""))
        except ValueError:
            return "[redacted URL]"
    return value


def preserve_secrets(proposed: Any, existing: Any) -> Any:
    """An unchanged redacted form keeps the stored credential on an edit."""
    if isinstance(proposed, dict) and isinstance(existing, dict):
        return {k: preserve_secrets(v, existing.get(k)) for k, v in proposed.items()}
    if existing is not None and (proposed == SECRET or proposed == redact(existing)):
        return existing
    return proposed


class SourceInput(BaseModel):
    kind: str
    modality: Modality = Modality.VIDEO
    enabled: bool = True
    rate_hz: float = Field(default=1.0, ge=0.01, le=120)
    label: str | None = Field(default=None, max_length=200)
    options: dict[str, Any] = Field(default_factory=dict)


class EnableInput(BaseModel):
    enabled: bool


class SourceManager:
    def __init__(self, service: Any) -> None:
        self.service = service
        self.path: Path = service.settings.data_dir / "sources.json"
        self.saved: dict[str, dict[str, Any]] = self._load()
        self.changed: set[str] = set()
        self.status: dict[str, dict[str, Any]] = {}
        self.test_lock = asyncio.Lock()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        # Invalid state is explicit, rather than silently reverting active cameras.
        data = json.loads(self.path.read_text())
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("invalid sources.json; expected version 1")
        if data.get("tenant_id") != self.service.settings.tenant_id:
            raise ValueError("sources.json belongs to a different deployment tenant")
        return {entry["source_id"]: entry for entry in data["sources"]}

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(
                {
                    "version": 1,
                    "tenant_id": self.service.settings.tenant_id,
                    "sources": list(self.saved.values()),
                },
                handle,
                indent=2,
            )
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)
        self.path.chmod(0o600)

    def configured(self, defaults: list[Connector]) -> list[Connector]:
        configs = {c.source_id: asdict(c.config) for c in defaults}
        configs.update(self.saved)
        built = []
        defaults_by_id = {c.source_id: c for c in defaults}
        for source_id, data in configs.items():
            if not data.get("enabled", True):
                self.status[source_id] = {"status": "disabled"}
                continue
            try:
                connector = defaults_by_id.get(source_id) if source_id not in self.saved else None
                connector = connector or build_connector(ConnectorConfig(**data))
                built.append(connector)
                self.status[source_id] = {"status": "starting"}
            except Exception:
                self.failed(
                    source_id, "Configuration could not be loaded; check connector kind and options"
                )
        return built

    def failed(self, source_id: str, message: str) -> None:
        self.status.setdefault(source_id, {}).update(status="error", error=message)

    @staticmethod
    def sample(observation: Observation) -> dict[str, Any]:
        payload = redact(observation.payload)
        encoded = json.dumps(payload, default=str)
        if len(encoded) > 4000:
            payload = {
                "summary": "Observation payload exceeds preview limit",
                "keys": list(observation.payload)[:25],
            }
        return {
            "source_id": observation.source_id,
            "modality": str(observation.modality),
            "ts": observation.ts.isoformat(),
            "payload": payload,
            "media_status": "processing" if observation.raw_ref else "unavailable",
        }

    def observed(self, connector: Connector, observation: Observation) -> None:
        self.status.setdefault(connector.source_id, {}).update(
            status="connected",
            last_success=utc_now().isoformat(),
            error=None,
            last_observation=self.sample(observation),
        )

    def _check_tenant(self) -> None:
        if current_tenant() != self.service.settings.tenant_id:
            raise HTTPException(403, "Sources are managed by their deployment tenant")

    def _entry(self, source_id: str) -> dict[str, Any]:
        if source_id in self.saved:
            return self.saved[source_id]
        for connector in self.service.connectors:
            if connector.source_id == source_id:
                return asdict(connector.config)
        raise HTTPException(404, "Source not found")

    def view(self, source_id: str, config: dict[str, Any]) -> dict[str, Any]:
        active = next((c for c in self.service.connectors if c.source_id == source_id), None)
        status = self.status.get(source_id, {})
        return {
            **redact(config),
            "status": status.get("status", "pending_restart"),
            "active": active is not None,
            "active_enabled": bool(active),
            "data_mode": "simulated" if config["kind"] == "simulator" else "real",
            "restart_required": source_id in self.changed,
            "last_success": status.get("last_success"),
            "last_observation": status.get("last_observation"),
            "error": status.get("error"),
            "last_test": status.get("last_test"),
        }

    async def listing(self) -> dict[str, Any]:
        self._check_tenant()
        configs = {c.source_id: asdict(c.config) for c in self.service.connectors}
        configs.update(self.saved)
        return {
            "sources": [self.view(k, v) for k, v in configs.items()],
            "registered_kinds": connector_kinds(),
            "restart_required": bool(self.changed),
            "note": "Saved changes apply after restarting ingest. Connection tests do not publish observations.",
        }

    def save(self, source_id: str, body: SourceInput) -> dict[str, Any]:
        self._check_tenant()
        if not _SOURCE_ID.fullmatch(source_id):
            raise HTTPException(
                422, "Source ID must use letters, numbers, dots, hyphens or underscores"
            )
        if body.kind not in connector_kinds():
            raise HTTPException(422, "Unknown connector kind")
        data = {"source_id": source_id, **body.model_dump(mode="json")}
        with contextlib.suppress(HTTPException):
            data["options"] = preserve_secrets(
                data["options"], self._entry(source_id).get("options", {})
            )
        if body.kind == "camera_rtsp":
            url = str(data["options"].get("url", ""))
            if not url.startswith(("rtsp://", "rtsps://")):
                raise HTTPException(422, "Camera requires an rtsp:// or rtsps:// options.url")
            if body.modality != Modality.VIDEO:
                raise HTTPException(422, "RTSP cameras must use video modality")
        # Validate constructors before saving without connecting to a device.
        try:
            build_connector(ConnectorConfig(**data))
        except Exception as exc:
            raise HTTPException(422, f"Invalid connector options ({type(exc).__name__})") from exc
        self.saved[source_id] = data
        self._persist()
        self.changed.add(source_id)
        return self.view(source_id, data)

    async def test(self, source_id: str) -> dict[str, Any]:
        self._check_tenant()
        data = self._entry(source_id)
        if self.test_lock.locked():
            raise HTTPException(409, "Another source test is running")
        if data["kind"] == "camera_rtsp":
            data = {**data, "options": {**data.get("options", {}), "store_frames": False}}
        connector = build_connector(ConnectorConfig(**data))
        sample = None
        async with self.test_lock:
            try:
                async with asyncio.timeout(12):
                    await connector.start()
                    observations = connector.observations()
                    try:
                        observation = await anext(observations)
                        sample = self.sample(observation)
                    finally:
                        await observations.aclose()
                result = {
                    "ok": True,
                    "tested_at": utc_now().isoformat(),
                    "sample": sample,
                    "message": "Received a sample observation; nothing was published",
                }
            except TimeoutError:
                result = {
                    "ok": False,
                    "tested_at": utc_now().isoformat(),
                    "sample": None,
                    "message": "No observation within 12 seconds; check address, device and sampling rate",
                }
            except Exception as exc:
                # Third-party exceptions can include connection strings and credentials.
                result = {
                    "ok": False,
                    "tested_at": utc_now().isoformat(),
                    "sample": None,
                    "message": f"Connection test failed ({type(exc).__name__}); check connection settings and installed connector dependencies",
                }
            finally:
                with contextlib.suppress(Exception):
                    await connector.stop()
            self.status.setdefault(source_id, {})["last_test"] = result
            return result

    def routes(self, app: FastAPI) -> None:
        app.add_api_route("/sources", self.listing, methods=["GET"], tags=["ingest"])
        app.add_api_route("/sources/{source_id}", self.save, methods=["PUT"], tags=["ingest"])
        app.add_api_route("/sources/{source_id}/test", self.test, methods=["POST"], tags=["ingest"])

        @app.post("/sources/{source_id}/enabled", tags=["ingest"])
        async def enabled(source_id: str, body: EnableInput) -> dict[str, Any]:
            self._check_tenant()
            data = {**self._entry(source_id), "enabled": body.enabled}
            return self.save(source_id, SourceInput.model_validate(data))
