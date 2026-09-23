"""Reviewed, single-source activation with a durable rollback checkpoint.

Only samples produced by the replacement connector after activation begins and
successfully published by ingest can satisfy verification. A checkpoint written
before touching the running connector makes process interruption fail closed on
restart. Credentials remain in the mode-0600 source store, never in responses.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from sio_schemas import Observation, utc_now

from .connectors.base import Connector, ConnectorConfig, build_connector

if TYPE_CHECKING:
    from .source_manager import SourceManager


class ActivationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_id: str = Field(min_length=20, max_length=100)
    timeout_s: float = Field(default=20, ge=3, le=30, allow_inf_nan=False)


class SourceActivation:
    preview_ttl_s = 300
    recovery_timeout_s = 12

    def __init__(self, manager: SourceManager) -> None:
        self.manager = manager
        self.service = manager.service
        self.lock = asyncio.Lock()
        self.key = secrets.token_bytes(32)
        self.previews: dict[str, dict[str, Any]] = {}
        self.waiters: dict[str, tuple[Connector, Any, asyncio.Future[Observation]]] = {}
        self.unsafe: set[str] = set()

    def _fingerprint(self, value: Any) -> str:
        # A per-process keyed digest binds even masked credentials without exposing
        # a stable offline password guessing oracle in the preview response.
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
        return hmac.new(self.key, encoded, hashlib.sha256).hexdigest()

    def active_config(self, source_id: str) -> dict[str, Any] | None:
        connector = next((c for c in self.service.connectors if c.source_id == source_id), None)
        return asdict(connector.config) if connector else None

    def ensure_idle(self) -> None:
        if self.lock.locked():
            raise HTTPException(
                409, "Another source activation is in progress; wait for its result"
            )

    def preview(self, source_id: str, *, rollback: bool = False) -> dict[str, Any]:
        from .source_manager import redact

        self.manager._check_tenant()
        self.ensure_idle()
        if source_id in self.unsafe:
            raise HTTPException(
                409, "Connector cleanup is incomplete; restart ingestion before retrying"
            )
        saved = deepcopy(self.manager._entry(source_id))
        before = self.active_config(source_id)
        record = self.manager.activation_records.get(source_id, {})
        if rollback:
            if not record.get("rollback_available"):
                raise HTTPException(409, "No verified activation is available to roll back")
            after = deepcopy(record["rollback_config"])
        else:
            after = saved
        if any(config and config["kind"] == "simulator" for config in (before, after)):
            raise HTTPException(422, "Simulator lifecycle changes require an ingestion restart")
        now = utc_now()
        token = secrets.token_urlsafe(32)
        self.previews = {k: v for k, v in self.previews.items() if v["deadline"] > time.monotonic()}
        if len(self.previews) >= 100:
            self.previews.pop(next(iter(self.previews)))
        action = "rollback" if rollback else "activate"
        self.previews[token] = {
            "source_id": source_id,
            "action": action,
            "deadline": time.monotonic() + self.preview_ttl_s,
            "fingerprint": self._fingerprint(
                [saved, before, self.manager.revisions.get(source_id, 0), record]
            ),
            "after": after,
        }
        keys = sorted(set(before or {}) | set(after))
        changed = [key for key in keys if (before or {}).get(key) != after.get(key)]
        requires_sample = bool(after.get("enabled", True))
        warnings = [
            "Only this source will be restarted. Activation briefly interrupts its incoming data.",
            "A fresh published observation verifies receipt, not camera coverage or detection accuracy.",
        ]
        if not requires_sample:
            warnings.append(
                "This change disables the source. No live-data verification will be claimed."
            )
        if after["kind"] == "camera_rtsp" and not after.get("options", {}).get(
            "store_frames", True
        ):
            warnings.append(
                "Frame storage is disabled; downstream video analysis will not receive images."
            )
        return {
            "preview_id": token,
            "source_id": source_id,
            "action": action,
            "expires_at": (now + timedelta(seconds=self.preview_ttl_s)).isoformat(),
            "before": redact(before),
            "after": redact(after),
            "changed_fields": changed,
            "requires_fresh_observation": requires_sample,
            "warnings": warnings,
        }

    def observed(self, connector: Connector, observation: Observation) -> None:
        waiting = self.waiters.get(connector.source_id)
        if not waiting:
            return
        expected, since, future = waiting
        if (
            future.done()
            or connector is not expected
            or observation.source_id != connector.source_id
            or observation.modality != connector.config.modality
            or observation.ts.tzinfo is None
        ):
            return
        if observation.ts < since or observation.ts > utc_now() + timedelta(seconds=5):
            return
        if (
            connector.config.kind == "camera_rtsp"
            and connector.config.options.get("store_frames", True)
            and not observation.raw_ref
        ):
            return
        future.set_result(observation)

    def status(self, source_id: str) -> dict[str, Any]:
        self.manager._check_tenant()
        self.manager._entry(source_id)
        record = self.manager.activation_records.get(source_id, {})
        return {
            key: deepcopy(record.get(key))
            for key in (
                "activation_id",
                "source_id",
                "status",
                "message",
                "started_at",
                "verified_at",
                "sample",
                "rollback_available",
                "action",
            )
        }

    def _persist_config(self, source_id: str, config: dict[str, Any]) -> None:
        self.manager.saved[source_id] = deepcopy(config)
        self.manager.revisions[source_id] = self.manager.revisions.get(source_id, 0) + 1
        self.manager._persist()

    async def _replace(
        self, source_id: str, config: dict[str, Any], timeout_s: float
    ) -> Observation | None:
        await self.service.stop_source(source_id)
        if not config.get("enabled", True):
            self.manager.status[source_id] = {"status": "disabled"}
            return None
        connector = build_connector(ConnectorConfig(**deepcopy(config)))
        future: asyncio.Future[Observation] = asyncio.get_running_loop().create_future()
        self.waiters[source_id] = (connector, utc_now(), future)
        try:
            async with asyncio.timeout(timeout_s):
                await self.service.start_source(connector)
                return await future
        finally:
            self.waiters.pop(source_id, None)
            if not future.done():
                future.cancel()

    async def apply(
        self, source_id: str, body: ActivationInput, *, rollback: bool = False
    ) -> dict[str, Any]:
        self.manager._check_tenant()
        self.ensure_idle()
        if self.manager.test_lock.locked():
            raise HTTPException(409, "A source connection test is running; wait for its result")
        async with self.lock:
            preview = self.previews.pop(body.preview_id, None)
            action = "rollback" if rollback else "activate"
            old_record = deepcopy(self.manager.activation_records.get(source_id, {}))
            saved = deepcopy(self.manager._entry(source_id))
            before = self.active_config(source_id)
            fingerprint = self._fingerprint(
                [saved, before, self.manager.revisions.get(source_id, 0), old_record]
            )
            if (
                not preview
                or preview["source_id"] != source_id
                or preview["action"] != action
                or preview["deadline"] <= time.monotonic()
                or preview["fingerprint"] != fingerprint
            ):
                raise HTTPException(
                    409,
                    "This preview is stale or already used; preview the current configuration again",
                )
            after = preview["after"]
            # For a first activation there is no old connection to restore. Keep
            # the new source saved but disabled so a restart cannot retry it silently.
            fallback = deepcopy(before or {**saved, "enabled": False})
            record = {
                "activation_id": secrets.token_urlsafe(24),
                "source_id": source_id,
                "action": action,
                "status": "applying",
                "started_at": utc_now().isoformat(),
                "message": "Waiting for a fresh observation"
                if after.get("enabled", True)
                else "Stopping this source",
                "rollback_config": fallback,
                "rollback_available": False,
                "sample": None,
                "verified_at": None,
            }
            self.manager.activation_records[source_id] = record
            try:
                self.manager._persist()
            except Exception:
                self.manager.activation_records[source_id] = old_record
                raise HTTPException(
                    503, "Could not persist the activation checkpoint; source was not changed"
                ) from None
            try:
                sample = await self._replace(source_id, after, body.timeout_s)
                record.update(
                    status="rolled_back" if rollback else "verified" if sample else "disabled",
                    message="Previous source configuration restored"
                    if rollback
                    else "Fresh observation published; source activated"
                    if sample
                    else "Source disabled; no live observation claimed",
                    rollback_available=not rollback,
                    sample=self.manager.sample(sample) if sample else None,
                    verified_at=utc_now().isoformat() if sample else None,
                )
                self._persist_config(source_id, after)
                self.manager.changed.discard(source_id)
                return self.status(source_id)
            except (Exception, asyncio.CancelledError) as error:
                # Recovery is deliberately shielded from a disconnected HTTP
                # caller. The global mutation lock remains held until it finishes.
                recovery = asyncio.create_task(self._recover(source_id, fallback, record, error))
                try:
                    await asyncio.shield(recovery)
                except asyncio.CancelledError:
                    await recovery
                if isinstance(error, asyncio.CancelledError):
                    raise
                return self.status(source_id)

    async def _recover(
        self, source_id: str, fallback: dict[str, Any], record: dict[str, Any], error: BaseException
    ) -> None:
        persistence_error = False
        try:
            # Store the known prior configuration first, even if restarting its
            # device fails, so the next process will not boot the rejected change.
            record.update(
                status="recovering", rollback_available=False, verified_at=None, sample=None
            )
            try:
                self._persist_config(source_id, fallback)
            except Exception:
                # Disk failure must not leave the rejected connector running.
                # The original durable checkpoint is still marked applying.
                persistence_error = True
            sample = await self._replace(source_id, fallback, self.recovery_timeout_s)
            record.update(
                status="rolled_back",
                message=f"Activation failed ({type(error).__name__}); previous configuration restored"
                if fallback.get("enabled", True)
                else f"Activation failed ({type(error).__name__}); source saved disabled",
                sample=self.manager.sample(sample) if sample else None,
                verified_at=utc_now().isoformat() if sample else None,
            )
            self.manager.changed.discard(source_id)
            if persistence_error:
                record["message"] += "; recovery persistence is being retried"
        except Exception as recovery_error:
            self.unsafe.add(source_id)
            record.update(
                status="rollback_failed",
                message=f"Activation and recovery failed ({type(recovery_error).__name__}); inspect source health before restarting ingestion",
                rollback_available=False,
                verified_at=None,
                sample=None,
            )
            self.manager.failed(source_id, record["message"])
        try:
            self.manager._persist()
        except Exception:
            self.unsafe.add(source_id)
            record.update(
                status="rollback_failed",
                message="Could not persist recovery; inspect source storage and restart ingestion",
                rollback_available=False,
            )
            self.manager.failed(source_id, record["message"])
