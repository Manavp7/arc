"""Source configuration, privacy and the real-camera ingestion handoff."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from sio_ingest.connectors.base import Connector, ConnectorConfig
from sio_ingest.service import IngestService
from sio_ingest.source_manager import SourceInput, SourceManager

from sio_core.authn import DevJwtAuth
from sio_core.tenancy import tenant_scope
from sio_schemas import Modality, Observation, Topic


def config() -> SourceInput:
    return SourceInput(
        kind="camera_rtsp",
        modality=Modality.VIDEO,
        options={
            "url": "rtsp://admin:camera-password@camera.local/live?token=abc",
            "password": "secret-pass",
        },
    )


async def test_saved_source_redacts_secrets_and_survives_restart(settings, memory_bus) -> None:
    service = IngestService(settings, bus=memory_bus)
    manager = service.sources
    with tenant_scope(settings.tenant_id):
        result = manager.save("gate-camera", config())
        assert result["restart_required"]
        assert not result["active"]
        assert "camera-password" not in json.dumps(result)
        assert "secret-pass" not in json.dumps(result)
        assert "abc" not in json.dumps(result)
        # Editing labels through a redacted form must keep camera credentials.
        manager.save("gate-camera", SourceInput.model_validate({**result, "label": "Front gate"}))
        restarted = SourceManager(service)
    assert restarted.saved["gate-camera"]["options"] == config().options
    assert manager.path.stat().st_mode & 0o777 == 0o600
    assert restarted.saved["gate-camera"]["label"] == "Front gate"
    assert not restarted.changed


async def test_source_configuration_does_not_cross_tenants(settings, memory_bus) -> None:
    service = IngestService(settings, bus=memory_bus)
    from fastapi import HTTPException

    with tenant_scope("another-tenant"), pytest.raises(HTTPException) as raised:
        await service.sources.listing()
    assert raised.value.status_code == 403


def test_source_mutations_require_integrator_role(settings, memory_bus) -> None:
    settings.audit_enabled = False
    service = IngestService(settings, bus=memory_bus)
    with TestClient(service.app) as client:
        operator = DevJwtAuth(settings).issue(subject="operator", roles=("operator",))
        response = client.put(
            "/sources/camera",
            json=config().model_dump(mode="json"),
            headers={"Authorization": f"Bearer {operator}"},
        )
        assert response.status_code == 403
        integrator = DevJwtAuth(settings).issue(subject="integrator", roles=("integrator",))
        headers = {"Authorization": f"Bearer {integrator}"}
        response = client.put(
            "/sources/camera", json=config().model_dump(mode="json"), headers=headers
        )
        assert response.status_code == 200
        disabled = client.post("/sources/camera/enabled", json={"enabled": False}, headers=headers)
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False
        assert disabled.json()["restart_required"] is True


async def test_real_frame_reaches_bus_without_simulator_rendering(settings, memory_bus) -> None:
    observation = Observation(
        source_id="gate-camera",
        modality=Modality.VIDEO,
        raw_ref="pending/tenants/default/frames/gate-camera/1.jpg",
    )

    class Camera(Connector):
        async def observations(self):
            yield observation
            raise asyncio.CancelledError

    service = IngestService(settings, bus=memory_bus)
    service.renderer.render = Mock(side_effect=AssertionError("must not render real frames"))
    camera = Camera(
        ConnectorConfig(source_id="gate-camera", kind="camera_rtsp", modality=Modality.VIDEO)
    )
    service._unmapped_warned = set()
    with pytest.raises(asyncio.CancelledError):
        await service._pump(camera)
    messages = await memory_bus.read_range(Topic.RAW_FRAMES)
    assert len(messages) == 1
    assert messages[0].decode(Observation).raw_ref == observation.raw_ref
    assert service.sources.status["gate-camera"]["last_success"]
    assert "raw_ref" not in service.sources.status["gate-camera"]["last_observation"]


async def test_connection_test_does_not_publish_or_expose_secrets(
    settings, memory_bus, monkeypatch
) -> None:
    service = IngestService(settings, bus=memory_bus)
    service.sources.save("gate-camera", config())

    class FakeCamera(Connector):
        async def observations(self):
            yield Observation(
                source_id="gate-camera",
                modality=Modality.VIDEO,
                payload={"password": "secret-pass", "width": 640},
            )

    fake = FakeCamera(
        ConnectorConfig(source_id="gate-camera", kind="camera_rtsp", modality=Modality.VIDEO)
    )
    fake.start = AsyncMock()
    fake.stop = AsyncMock()
    monkeypatch.setattr("sio_ingest.source_manager.build_connector", lambda config: fake)
    result = await service.sources.test("gate-camera")
    assert result["ok"]
    assert result["sample"]["payload"]["width"] == 640
    assert "secret-pass" not in json.dumps(result)
    assert await memory_bus.read_range(Topic.RAW_FRAMES) == []
    fake.stop.assert_awaited_once()


async def test_pending_frame_promoted_only_after_redaction(settings, memory_bus) -> None:
    import numpy as np
    from sio_perception.service import PerceptionService

    service = PerceptionService(settings, bus=memory_bus)
    service.blob.put = AsyncMock()
    service.blob.delete = AsyncMock()
    service.redactor.apply = Mock(return_value=(np.zeros((16, 16, 3), dtype=np.uint8), 0))
    observation = Observation(
        source_id="gate-camera",
        modality=Modality.VIDEO,
        raw_ref="pending/tenants/default/frames/gate-camera/1.jpg",
    )
    await service._store_redacted(observation, np.zeros((16, 16, 3), dtype=np.uint8), [])
    service.blob.put.assert_awaited_once()
    assert service.blob.put.call_args.args[0] == "tenants/default/frames/gate-camera/1.jpg"
    service.blob.delete.assert_awaited_once_with("pending/tenants/default/frames/gate-camera/1.jpg")
    assert observation.raw_ref == "tenants/default/frames/gate-camera/1.jpg"


async def test_worldmodel_waits_for_sanitized_frame(settings, memory_bus) -> None:
    from sio_worldmodel.service import WorldModelService

    from sio_core import MessageContext
    from sio_schemas import BusMessage

    service = WorldModelService(settings, bus=memory_bus)
    service.blob.get = AsyncMock(side_effect=AssertionError("must not read private buffer"))
    observation = Observation(
        source_id="camera",
        modality=Modality.VIDEO,
        raw_ref="pending/tenants/default/frames/camera/x.jpg",
    )
    message = BusMessage.of(Topic.RAW_FRAMES, observation)
    await service.on_message(message, MessageContext(service, message))
    service.blob.get.assert_not_awaited()
    assert Topic.FRAMES_READY in service.subscribes


async def test_inference_failure_does_not_rate_limit_its_retry(settings, memory_bus) -> None:
    from sio_perception.service import PerceptionService

    from sio_core import MessageContext
    from sio_schemas import BusMessage

    service = PerceptionService(settings, bus=memory_bus)
    service._infer = AsyncMock(side_effect=[RuntimeError("temporary decoder error"), []])
    observation = Observation(source_id="camera", modality=Modality.VIDEO)
    message = BusMessage.of(Topic.RAW_FRAMES, observation)
    with pytest.raises(RuntimeError):
        await service.on_message(message, MessageContext(service, message))
    await service.on_message(message, MessageContext(service, message))
    assert service._infer.await_count == 2
