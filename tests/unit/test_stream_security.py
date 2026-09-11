"""Tenant isolation, authentication and identity propagation regression tests."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sio_api.app import ApiService
from sio_api.stream import StreamHub, Subscriber
from starlette.websockets import WebSocketDisconnect

from sio_core.authn import DevJwtAuth
from sio_schemas import BusMessage, Event, Topic


def envelope(tenant: str) -> BusMessage:
    return BusMessage.of(Topic.EVENTS, Event(tenant_id=tenant, type="entity_appeared"))


def test_subscribers_filter_tenant_before_queueing() -> None:
    subscriber = Subscriber(None, tenant_id="tenant-a", maxsize=1)
    subscriber.offer(envelope("tenant-a"))
    subscriber.offer(envelope("tenant-b"))
    assert subscriber.queue.qsize() == 1
    assert subscriber.queue.get_nowait().tenant_id == "tenant-a"
    assert subscriber.dropped == 0


async def test_sse_closes_at_session_expiry(memory_bus) -> None:
    hub = StreamHub(memory_bus)
    with hub.subscribe(tenant_id="tenant-a") as subscriber:
        frames = [frame async for frame in hub.events(subscriber, expires_at=time.time() - 1)]
    assert frames == [": connected\n\n"]


@pytest.mark.parametrize("path", ["/ws", "/graphql"])
def test_websockets_reject_anonymous_and_bad_credentials(settings, memory_bus, path) -> None:
    settings.audit_enabled = False
    service = ApiService(settings, bus=memory_bus)
    with TestClient(service.app) as client:
        for headers in ({}, {"Authorization": "Bearer invalid"}):
            with pytest.raises(WebSocketDisconnect) as raised:
                with client.websocket_connect(path, headers=headers):
                    pass
            assert raised.value.code == 4401


@pytest.mark.parametrize("cookie", [False, True])
def test_websocket_uses_authenticated_tenant(settings, memory_bus, cookie) -> None:
    settings.audit_enabled = False
    service = ApiService(settings, bus=memory_bus)
    token = DevJwtAuth(settings).issue(subject="alice", tenant_id="tenant-a", roles=("operator",))
    with TestClient(service.app) as client:
        headers = (
            {"cookie": f"sio_token={token}"} if cookie else {"Authorization": f"Bearer {token}"}
        )
        with client.websocket_connect("/ws", headers=headers):
            assert len(service.hub.subscribers) == 1
            assert next(iter(service.hub.subscribers)).tenant_id == "tenant-a"


@pytest.mark.parametrize("cookie", [False, True])
@pytest.mark.parametrize(
    "path",
    [
        "/api/alerts/a1",
        "/api/forecasts/latest",
        "/api/workflow/playbooks",
        "/api/agents",
        "/api/agents/cycles",
        "/api/search/frames?q=truck",
        "/api/sources",
    ],
)
def test_gateway_keeps_caller_credentials(settings, memory_bus, monkeypatch, cookie, path) -> None:
    settings.audit_enabled = False
    service = ApiService(settings, bus=memory_bus)
    token = DevJwtAuth(settings).issue(subject="alice", tenant_id="tenant-a", roles=("admin",))
    forward = AsyncMock(return_value=httpx.Response(200, json={"ok": True}))
    monkeypatch.setattr(httpx.AsyncClient, "request", forward)
    with TestClient(service.app) as client:
        headers = (
            {"cookie": f"sio_token={token}"} if cookie else {"Authorization": f"Bearer {token}"}
        )
        response = client.get(path, headers=headers)
    assert response.status_code == 200
    assert forward.call_args.kwargs["headers"] == {"Authorization": f"Bearer {token}"}


def test_auth_config_supports_production_login_without_a_token(settings, memory_bus) -> None:
    settings.auth_mode = "keycloak"
    settings.audit_enabled = False
    service = ApiService(settings, bus=memory_bus)
    with TestClient(service.app) as client:
        response = client.get("/auth/config")
    assert response.status_code == 200
    assert response.json()["mode"] == "keycloak"
    assert response.json()["oidc"]["client_id"] == settings.keycloak_client_id
    assert "secret" not in response.text


def test_pending_and_other_tenant_media_are_never_served(settings, memory_bus) -> None:
    settings.audit_enabled = False
    service = ApiService(settings, bus=memory_bus)
    service.blob.get = AsyncMock(return_value=b"image")
    token = DevJwtAuth(settings).issue(subject="alice", tenant_id="tenant-a", roles=("operator",))
    with TestClient(service.app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        for key in (
            "pending/tenants/tenant-a/frames/x.jpg",
            "tenants/tenant-b/frames/x.jpg",
            "frames/legacy.jpg",
        ):
            assert client.get(f"/media/{key}", headers=headers).status_code == 404
        response = client.get("/media/tenants/tenant-a/frames/x.jpg", headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert service.blob.get.await_count == 1


def test_websocket_rejects_untrusted_browser_origin(settings, memory_bus) -> None:
    settings.audit_enabled = False
    service = ApiService(settings, bus=memory_bus)
    token = DevJwtAuth(settings).issue(subject="alice", roles=("operator",))
    with TestClient(service.app) as client:
        with pytest.raises(WebSocketDisconnect) as raised:
            with client.websocket_connect(
                "/ws",
                headers={"cookie": f"sio_token={token}", "origin": "https://untrusted.example"},
            ):
                pass
    assert raised.value.code == 4403


def test_domain_service_rejects_other_tenant_instead_of_using_default(settings, memory_bus) -> None:
    from sio_ingest.service import IngestService

    settings.audit_enabled = False
    service = IngestService(settings, bus=memory_bus)
    token = DevJwtAuth(settings).issue(subject="foreign-admin", tenant_id="other", roles=("admin",))
    with TestClient(service.app) as client:
        response = client.get("/site", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.json()["rule"] == "tenant-isolation"


def test_replay_registry_does_not_list_read_or_cancel_other_tenant() -> None:
    from datetime import timedelta

    from sio_api.timeline import ReplayRegistry, plan_replay

    from sio_schemas import utc_now

    registry = ReplayRegistry()
    end = utc_now()
    session = plan_replay(tenant_id="owner", start=end - timedelta(minutes=1), end=end)
    registry.add(session)
    assert registry.get(session.replay_id, tenant_id="other") is None
    assert registry.cancel(session.replay_id, tenant_id="other") is False
    assert registry.describe(tenant_id="other")["active"] == []
    assert registry.get(session.replay_id, tenant_id="owner") == session
