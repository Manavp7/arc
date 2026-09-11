"""Exact historical event lookup keeps the authenticated tenant and zone boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sio_api.app import ApiService

from sio_core.authn import DevJwtAuth
from sio_schemas import Event


@pytest.fixture
def event_api(settings, memory_bus):
    settings.audit_enabled = False
    settings.auth_mode = "dev"
    service = ApiService(settings, bus=memory_bus)
    event = Event(
        event_id="evt-historical",
        tenant_id="tenant-a",
        type="entity_appeared",
        zone_id="allowed-zone",
        ts=datetime(2020, 1, 1, tzinfo=UTC),
    )

    async def fetchrow(sql, params):
        assert "WHERE tenant_id = %s AND event_id = %s" in sql
        return (
            {"payload": event.model_dump(mode="json")}
            if params == ("tenant-a", event.event_id)
            else None
        )

    service.pool = SimpleNamespace(fetchrow=AsyncMock(side_effect=fetchrow))
    return service, event


def headers(service, tenant="tenant-a", zones=()):
    token = DevJwtAuth(service.settings).issue(
        subject="reviewer", tenant_id=tenant, roles=("operator",), clearance=3, zones=zones
    )
    return {"Authorization": f"Bearer {token}"}


async def test_exact_event_is_available_without_a_recent_list_window(event_api):
    service, event = event_api
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(service.app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/events/{event.event_id}", headers=headers(service, zones=("allowed-zone",))
        )
    assert response.status_code == 200
    assert response.json()["event_id"] == event.event_id
    assert response.json()["ts"].startswith("2020-01-01")
    service.pool.fetchrow.assert_awaited_once()
    assert service.pool.fetchrow.await_args.args[1] == ("tenant-a", event.event_id)


async def test_exact_event_rejects_other_tenant_missing_and_unpermitted_zone(event_api):
    service, event = event_api
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(service.app), base_url="http://test"
    ) as client:
        assert (await client.get(f"/api/events/{event.event_id}")).status_code == 401
        service.pool.fetchrow.assert_not_awaited()
        assert (
            await client.get(
                f"/api/events/{event.event_id}", headers=headers(service, tenant="tenant-b")
            )
        ).status_code == 404
        assert (
            await client.get("/api/events/not-present", headers=headers(service))
        ).status_code == 404
        denied = await client.get(
            f"/api/events/{event.event_id}", headers=headers(service, zones=("different-zone",))
        )
    assert denied.status_code == 403
    assert event.event_id not in denied.text
