"""Actor/scope and cursor contracts without infrastructure; SQL races live in integration."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from sio_alerts.outbox import AlertOutbox, decode_cursor, encode_cursor
from sio_alerts.service import AlertsService
from sio_decision.service import DecisionService

from sio_core.authn import DevJwtAuth
from sio_schemas import Alert, ApprovalState, Decision


@asynccontextmanager
async def no_transaction():
    yield


@pytest.mark.parametrize("action,actor_field", [("ack", "ack_by"), ("resolve", "resolved_by")])
async def test_alert_actor_is_verified_not_body(settings, action, actor_field):
    service = AlertsService(settings)
    service._mutation = no_transaction
    service._load = AsyncMock(
        return_value=Alert(
            tenant_id=settings.tenant_id, title="Test", group_key="test", zone_id="yard"
        )
    )
    service._persist = AsyncMock(return_value=True)
    service._emit = AsyncMock()
    token = DevJwtAuth(settings).issue(subject="alice", roles=("operator",), zones=("yard",))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service.app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/alerts/id/{action}",
                json={actor_field: "forged-bob"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 200, response.text
        saved = service._persist.call_args.args[0]
        assert "alice" in str(saved.explanation.notes)
        assert "forged-bob" not in saved.to_json()
        if action == "ack":
            assert saved.ack_by == "alice"
    finally:
        await service.client.aclose()


@pytest.mark.parametrize("zone", [None, "private"])
@pytest.mark.parametrize(
    "path,method",
    [
        ("/alerts/id", "get"),
        ("/alerts/id/ack", "post"),
        ("/alerts/id/resolve", "post"),
        ("/alerts/id/escalate", "post"),
    ],
)
async def test_scoped_alert_detail_and_actions_refuse_invisible_zone(settings, zone, path, method):
    service = AlertsService(settings)
    service._mutation = no_transaction
    service._load = AsyncMock(
        return_value=Alert(
            tenant_id=settings.tenant_id, title="Hidden", group_key="test", zone_id=zone
        )
    )
    service._persist = AsyncMock()
    token = DevJwtAuth(settings).issue(subject="alice", roles=("operator",), zones=("yard",))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service.app), base_url="http://test"
        ) as client:
            response = await client.request(
                method,
                path,
                headers={"Authorization": f"Bearer {token}"},
                **({"json": {}} if method == "post" else {}),
            )
        assert response.status_code == 404, response.text
        service._persist.assert_not_awaited()
    finally:
        await service.client.aclose()


@pytest.mark.parametrize("action,field", [("approve", "approved_by"), ("reject", "rejected_by")])
async def test_decision_actor_is_verified(settings, action, field):
    service = DecisionService(settings)
    service._load = AsyncMock(
        return_value=Decision(
            tenant_id=settings.tenant_id,
            rationale="Authored request",
            approval=ApprovalState.PENDING,
        )
    )
    service._persist = AsyncMock()
    service._emit = AsyncMock()
    token = DevJwtAuth(settings).issue(subject="commander-alice", roles=("commander",), clearance=3)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=service.app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/decisions/id/{action}",
            json={field: "forged-bob"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200, response.text
    assert service._persist.call_args.args[0].approved_by == "commander-alice"


def test_cursor_is_bound_to_tenant_scope_and_filters():
    context = ["deliveries-v1", "tenant", "failed", None, ["yard"]]
    cursor = encode_cursor(context, ["2026-01-01T00:00:00+00:00", "id"])
    assert decode_cursor(cursor, context)[1] == "id"
    with pytest.raises(HTTPException):
        decode_cursor(cursor, ["deliveries-v1", "other", "failed", None, ["yard"]])
    with pytest.raises(HTTPException):
        decode_cursor("not a cursor", context)


async def test_history_checks_zone_before_fetching_attempts():
    pool = AsyncMock()
    pool.fetchrow.return_value = None
    with pytest.raises(HTTPException) as denied:
        await AlertOutbox(pool, AsyncMock()).history("tenant", "hidden", allowed_zones=("yard",))
    assert denied.value.status_code == 404
    pool.fetch.assert_not_awaited()
