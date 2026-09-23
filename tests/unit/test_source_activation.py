"""Activation exercises real ingest tasks, publication and durable recovery with authored inputs."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from typing import ClassVar
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sio_ingest.connectors.base import _REGISTRY, Connector, ConnectorConfig
from sio_ingest.service import IngestService
from sio_ingest.source_activation import ActivationInput
from sio_ingest.source_manager import SourceInput, SourceManager

from sio_core.authn import DevJwtAuth
from sio_core.tenancy import tenant_scope
from sio_schemas import Modality, Observation, Topic, utc_now


class ActivationFixture(Connector):
    kind = "activation_fixture"
    started: ClassVar[list[Connector]] = []
    stopped: ClassVar[list[Connector]] = []

    async def start(self):
        self.started.append(self)
        if self.config.options.get("start_error"):
            raise ValueError("password=private-camera-password")

    async def stop(self):
        self.stopped.append(self)

    async def observations(self):
        mode = self.config.options.get("mode", "fresh")
        if mode == "stale":
            yield Observation(
                source_id=self.source_id, modality=Modality.IOT, ts=utc_now() - timedelta(hours=1)
            )
        elif mode == "fresh":
            yield Observation(
                source_id=self.source_id,
                modality=Modality.IOT,
                payload={"temperature": 18, "password": "private-camera-password"},
            )
        await asyncio.Event().wait()


@pytest.fixture
def service(settings, memory_bus, monkeypatch):
    monkeypatch.setitem(_REGISTRY, "activation_fixture", ActivationFixture)
    svc = IngestService(settings, bus=memory_bus)
    svc._unmapped_warned = set()
    return svc


def save(service, source_id="sensor-a", **options):
    return service.sources.save(
        source_id, SourceInput(kind="activation_fixture", modality=Modality.IOT, options=options)
    )


def ticket(service, source_id="sensor-a", rollback=False):
    preview = service.sources.activation.preview(source_id, rollback=rollback)
    return ActivationInput(preview_id=preview["preview_id"], timeout_s=3)


async def activate(service, source_id="sensor-a", rollback=False):
    return await service.sources.activation.apply(
        source_id, ticket(service, source_id, rollback), rollback=rollback
    )


async def test_activation_publishes_fresh_sample_without_touching_other_connectors(
    service, memory_bus
):
    other = ActivationFixture(
        ConnectorConfig(source_id="other", kind="activation_fixture", modality=Modality.IOT)
    )
    await service.start_source(other)
    try:
        save(service, password="private-camera-password")
        preview = service.sources.activation.preview("sensor-a")
        assert "private-camera-password" not in json.dumps(preview)
        result = await service.sources.activation.apply(
            "sensor-a", ActivationInput(preview_id=preview["preview_id"])
        )
        assert result["status"] == "verified"
        assert result["verified_at"]
        assert result["sample"]["payload"]["temperature"] == 18
        assert "private-camera-password" not in json.dumps(result)
        assert other in service.connectors
        assert other not in ActivationFixture.stopped
        assert service._connector_tasks["other"].done() is False
        assert "sensor-a" not in service.sources.changed
        rows = await memory_bus.read_range(Topic.RAW_IOT)
        assert any(row.decode(Observation).source_id == "sensor-a" for row in rows)
    finally:
        await service.teardown()


async def test_stale_preview_after_save_rejected_without_restart(service):
    save(service)
    body = ticket(service)
    save(service, mode="fresh")
    with pytest.raises(HTTPException, match="stale") as error:
        await service.sources.activation.apply("sensor-a", body)
    assert error.value.status_code == 409
    assert not service.connectors


async def test_preview_is_single_use_and_cannot_be_replayed_on_another_source(service):
    save(service)
    save(service, "sensor-b")
    body = ticket(service)
    with pytest.raises(HTTPException):
        await service.sources.activation.apply("sensor-b", body)
    with pytest.raises(HTTPException):
        await service.sources.activation.apply("sensor-a", body)


async def test_failure_restores_last_running_config_and_persists_it(service):
    save(service, revision_label="old", password="private-camera-password")
    await activate(service)
    old = deepcopy(service.sources.saved["sensor-a"])
    save(service, start_error=True, revision_label="new")
    try:
        result = await activate(service)
        assert result["status"] == "rolled_back"
        assert "ValueError" in result["message"]
        assert "private-camera-password" not in json.dumps(result)
        assert service.sources.saved["sensor-a"] == old
        assert service.connectors[0].config.options["revision_label"] == "old"
        restarted = SourceManager(service)
        assert restarted.saved["sensor-a"] == old
        assert not restarted.activation.status("sensor-a")["rollback_available"]
    finally:
        await service.teardown()


async def test_first_failed_activation_is_saved_disabled(service):
    save(service, start_error=True)
    result = await activate(service)
    assert result["status"] == "rolled_back"
    assert result["verified_at"] is None
    assert service.sources.saved["sensor-a"]["enabled"] is False
    assert not service.connectors
    assert SourceManager(service).saved["sensor-a"]["enabled"] is False


async def test_manual_rollback_survives_restart_of_manager(service):
    save(service, value="old")
    await activate(service)
    save(service, value="new")
    await activate(service)
    try:
        # Reload persisted rollback metadata while the same connector is active.
        service.sources = SourceManager(service)
        result = await activate(service, rollback=True)
        assert result["status"] == "rolled_back"
        assert result["verified_at"]
        assert result["rollback_available"] is False
        assert service.sources.saved["sensor-a"]["options"]["value"] == "old"
    finally:
        await service.teardown()


async def test_interrupted_activation_restores_checkpoint_before_startup(service):
    save(service, value="old")
    await activate(service)
    old = deepcopy(service.sources.saved["sensor-a"])
    save(service, value="rejected")
    record = service.sources.activation_records["sensor-a"]
    record.update(
        status="applying",
        rollback_config=old,
        sample={"old": True},
        verified_at="stale",
        rollback_available=True,
    )
    service.sources._persist()
    await service.teardown()
    restarted = SourceManager(service)
    assert restarted.saved["sensor-a"] == old
    result = restarted.activation.status("sensor-a")
    assert result["status"] == "recovered_after_restart"
    assert result["sample"] is None and result["verified_at"] is None
    assert result["rollback_available"] is False
    assert restarted.path.stat().st_mode & 0o777 == 0o600


async def test_cancellation_recovers_prior_source_and_holds_mutation_lock(service):
    save(service, value="old")
    await activate(service)
    save(service, mode="silent")
    task = asyncio.create_task(activate(service))
    for _ in range(100):
        if service.sources.activation.waiters:
            break
        await asyncio.sleep(0.001)
    assert service.sources.activation.lock.locked()
    with pytest.raises(HTTPException):
        save(service, value="racing-save")
    with pytest.raises(HTTPException):
        service.sources.activation.preview("sensor-a")
    with pytest.raises(HTTPException):
        await service.sources.test("sensor-a")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    try:
        assert not service.sources.activation.lock.locked()
        assert service.sources.activation.status("sensor-a")["status"] == "rolled_back"
        assert service.sources.saved["sensor-a"]["options"]["value"] == "old"
    finally:
        await service.teardown()


async def test_stale_old_and_wrong_source_observations_do_not_satisfy_waiter(service):
    config = ConnectorConfig(source_id="sensor-a", kind="activation_fixture", modality=Modality.IOT)
    expected = ActivationFixture(config)
    other = ActivationFixture(config)
    future = asyncio.get_running_loop().create_future()
    since = utc_now()
    service.sources.activation.waiters["sensor-a"] = (expected, since, future)
    service.sources.activation.observed(
        other, Observation(source_id="sensor-a", modality=Modality.IOT)
    )
    service.sources.activation.observed(
        expected, Observation(source_id="wrong", modality=Modality.IOT)
    )
    service.sources.activation.observed(
        expected,
        Observation(source_id="sensor-a", modality=Modality.IOT, ts=since - timedelta(seconds=1)),
    )
    service.sources.activation.observed(
        expected,
        Observation(source_id="sensor-a", modality=Modality.IOT, ts=since + timedelta(days=1)),
    )
    assert not future.done()
    service.sources.activation.observed(
        expected, Observation(source_id="sensor-a", modality=Modality.IOT)
    )
    assert future.done()


async def test_no_observation_does_not_become_ready_and_auto_disables(service):
    save(service, mode="stale")
    # Small timeout bypasses input validation solely to keep this unit test quick.
    body = ticket(service).model_copy(update={"timeout_s": 0.03})
    result = await service.sources.activation.apply("sensor-a", body)
    assert result["status"] == "rolled_back"
    assert result["verified_at"] is None
    assert result["sample"] is None
    assert not service.connectors


async def test_disable_has_no_freshness_claim_and_can_be_rolled_back(service):
    save(service)
    await activate(service)
    service.sources.save(
        "sensor-a", SourceInput(kind="activation_fixture", modality=Modality.IOT, enabled=False)
    )
    result = await activate(service)
    assert result["status"] == "disabled"
    assert result["verified_at"] is None and result["sample"] is None
    assert not service.connectors
    try:
        result = await activate(service, rollback=True)
        assert result["verified_at"]
        assert service.sources.saved["sensor-a"]["enabled"] is True
    finally:
        await service.teardown()


async def test_auth_tenant_and_roles_protect_activation(service, settings):
    settings.audit_enabled = False
    save(service)
    issuer = DevJwtAuth(settings)
    with TestClient(service.app) as client:
        for path in (
            "/sources/sensor-a/activation/preview",
            "/sources/sensor-a/activation",
            "/sources/sensor-a/activation/rollback-preview",
            "/sources/sensor-a/activation/rollback",
        ):
            assert client.post(path, json={}).status_code == 401
            token = issuer.issue(subject="operator", roles=("operator",))
            assert (
                client.post(path, json={}, headers={"Authorization": f"Bearer {token}"}).status_code
                == 403
            )
        token = issuer.issue(subject="integrator", roles=("integrator",))
        response = client.post(
            "/sources/sensor-a/activation/preview",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
    with tenant_scope("unrelated-tenant"), pytest.raises(HTTPException) as error:
        service.sources.activation.status("sensor-a")
    assert error.value.status_code == 403


async def test_checkpoint_persistence_failure_never_changes_running_source(service, monkeypatch):
    save(service)
    monkeypatch.setattr(
        service.sources, "_persist", lambda: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(HTTPException) as error:
        await activate(service)
    assert error.value.status_code == 503
    assert not service.connectors


async def test_unrecoverable_source_is_blocked_from_further_activation(service):
    save(service)
    await activate(service)
    save(service, start_error=True)
    service.start_source = AsyncMock(side_effect=RuntimeError("private-camera-password"))
    result = await activate(service)
    assert result["status"] == "rollback_failed"
    assert not result["verified_at"]
    assert "private-camera-password" not in json.dumps(result)
    with pytest.raises(HTTPException):
        service.sources.activation.preview("sensor-a")
    await service.teardown()


async def test_expired_preview_and_changed_active_connection_are_rejected(service):
    save(service)
    body = ticket(service)
    service.sources.activation.previews[body.preview_id]["deadline"] = 0
    with pytest.raises(HTTPException):
        await service.sources.activation.apply("sensor-a", body)
    body = ticket(service)
    connector = ActivationFixture(
        ConnectorConfig(source_id="sensor-a", kind="activation_fixture", modality=Modality.IOT)
    )
    await service.start_source(connector)
    try:
        with pytest.raises(HTTPException):
            await service.sources.activation.apply("sensor-a", body)
        assert service.connectors == [connector]
    finally:
        await service.teardown()


async def test_failed_publication_cannot_verify_activation(service, memory_bus):
    save(service)
    service.publish = AsyncMock(side_effect=RuntimeError("bus unavailable"))
    body = ticket(service).model_copy(update={"timeout_s": 0.03})
    result = await service.sources.activation.apply("sensor-a", body)
    assert result["status"] == "rolled_back"
    assert result["sample"] is None and result["verified_at"] is None
    assert not service.connectors
    assert await memory_bus.read_range(Topic.RAW_IOT) == []


async def test_rtsp_verification_requires_stored_frame_unless_explicitly_disabled(service):
    connector = ActivationFixture(
        ConnectorConfig(source_id="camera-a", kind="camera_rtsp", modality=Modality.VIDEO)
    )
    future = asyncio.get_running_loop().create_future()
    service.sources.activation.waiters["camera-a"] = (connector, utc_now(), future)
    service.sources.activation.observed(
        connector, Observation(source_id="camera-a", modality=Modality.VIDEO)
    )
    assert not future.done()
    service.sources.activation.observed(
        connector,
        Observation(source_id="camera-a", modality=Modality.VIDEO, raw_ref="pending/frame.jpg"),
    )
    assert future.done()


async def test_changed_masked_credentials_invalidate_preview_and_never_appear_in_views(service):
    save(service, password="first-secret")
    body = ticket(service)
    first = service.sources.activation.preview("sensor-a")
    save(service, password="second-secret")
    second = service.sources.activation.preview("sensor-a")
    assert first["after"] == second["after"]  # both correctly display masks
    with pytest.raises(HTTPException):
        await service.sources.activation.apply("sensor-a", body)
    for view in (first, second, await service.sources.listing()):
        assert "first-secret" not in json.dumps(view)
        assert "second-secret" not in json.dumps(view)


async def test_save_failure_leaves_previous_draft_and_preview_valid(service, monkeypatch):
    save(service, value="old")
    body = ticket(service)
    original = service.sources._persist
    monkeypatch.setattr(
        service.sources, "_persist", lambda: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(HTTPException):
        save(service, value="not-persisted")
    assert service.sources.saved["sensor-a"]["options"]["value"] == "old"
    monkeypatch.setattr(service.sources, "_persist", original)
    try:
        assert (await service.sources.activation.apply("sensor-a", body))["status"] == "verified"
    finally:
        await service.teardown()


async def test_commit_write_failure_stops_rejected_connector_even_with_broken_disk(
    service, monkeypatch
):
    save(service, value="old")
    await activate(service)
    save(service, value="new")
    original = service.sources._persist
    writes = 0

    def failing_commit():
        nonlocal writes
        writes += 1
        if writes > 1:
            raise OSError("disk full")
        original()

    monkeypatch.setattr(service.sources, "_persist", failing_commit)
    try:
        result = await activate(service)
        assert result["status"] == "rollback_failed"  # persistence remains broken
        assert service.connectors[0].config.options["value"] == "old"
        # Last durable applying checkpoint restores the old config next startup.
        restarted = SourceManager(service)
        assert restarted.saved["sensor-a"]["options"]["value"] == "old"
        assert restarted.activation.status("sensor-a")["status"] == "recovered_after_restart"
    finally:
        await service.teardown()


async def test_cancelled_rtsp_open_releases_eventual_capture(monkeypatch):
    import sys
    import threading
    from types import SimpleNamespace

    from sio_ingest.connectors.rtsp import RtspCameraConnector

    entered, finish, released = threading.Event(), threading.Event(), threading.Event()

    def open_capture(*_args):
        entered.set()
        assert finish.wait(3)
        return SimpleNamespace(release=released.set)

    fake_cv2 = SimpleNamespace(
        VideoCapture=open_capture,
        CAP_FFMPEG=1,
        CAP_GSTREAMER=2,
        CAP_PROP_OPEN_TIMEOUT_MSEC=3,
        CAP_PROP_READ_TIMEOUT_MSEC=4,
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)
    camera = RtspCameraConnector(
        ConnectorConfig(
            source_id="camera",
            kind="camera_rtsp",
            modality=Modality.VIDEO,
            options={"url": "rtsp://camera/stream", "store_frames": False},
        )
    )
    task = asyncio.create_task(camera.start())
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    finish.set()
    for _ in range(100):
        if released.is_set():
            break
        await asyncio.sleep(0.005)
    assert released.is_set()
    assert camera._capture is None


async def test_rtsp_stop_waits_for_inflight_reader_without_blocking_event_loop():
    import threading
    from types import SimpleNamespace

    from sio_ingest.connectors.rtsp import RtspCameraConnector

    entered, finish, released = threading.Event(), threading.Event(), threading.Event()

    def read_frame():
        entered.set()
        assert finish.wait(3)
        assert not released.is_set()
        return True, object()

    camera = RtspCameraConnector(
        ConnectorConfig(
            source_id="camera",
            kind="camera_rtsp",
            modality=Modality.VIDEO,
            options={"url": "rtsp://camera/stream", "store_frames": False},
        )
    )
    camera._capture = SimpleNamespace(read=read_frame, release=released.set)
    reader = asyncio.create_task(asyncio.to_thread(camera._read_frame))
    assert await asyncio.to_thread(entered.wait, 2)
    stop = asyncio.create_task(camera.stop())
    await asyncio.sleep(0.01)
    assert not stop.done() and not released.is_set()
    finish.set()
    await asyncio.wait_for(asyncio.gather(reader, stop), 2)
    assert released.is_set() and camera._capture is None
