"""Delivery behavior without a database or external HTTP endpoint.

The companion integration tests exercise real SQL atomicity, leases and recovery.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from sio_alerts.outbox import (
    MAX_ATTEMPTS,
    AlertOutbox,
    delivery_id,
    destination_label,
    retry_delay,
)
from sio_alerts.service import AlertsService

from sio_core.authn import DevJwtAuth
from sio_schemas import Alert, AlertState, Severity, utc_now

DESTINATION = "https://user:private-password@receiver.invalid/private-path?token=private-token"


def pending_row(**changes):
    return {
        "tenant_id": "demo",
        "delivery_id": "adl_test",
        "alert_id": "alert_test",
        "destination_url": DESTINATION,
        "payload": {"alert_id": "alert_test"},
        "status": "sending",
        "lease_token": "lease-one",
        "attempts": 1,
        "cycle_attempts": 1,
        **changes,
    }


@pytest.mark.parametrize(
    "status,expected",
    [
        (204, "delivered"),
        (302, "failed"),
        (400, "failed"),
        (401, "failed"),
        (408, "pending"),
        (429, "pending"),
        (503, "pending"),
    ],
)
async def test_http_result_is_classified_without_exposing_receiver_body(status, expected):
    requests = []

    def receiver(request):
        requests.append(request)
        return httpx.Response(
            status,
            text="secret receiver detail and private-token",
            headers={"location": "https://other.invalid/"},
        )

    pool = AsyncMock()
    pool.fetchrow.return_value = {"delivery_id": "adl_test"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(receiver)) as client:
        outbox = AlertOutbox(pool, client)
        outbox.claim = AsyncMock(return_value=pending_row())
        assert await outbox.dispatch_one("demo", DESTINATION) == expected
    assert len(requests) == 1, "redirects must not retarget an alert"
    assert requests[0].headers["Idempotency-Key"] == "adl_test"
    assert requests[0].headers["X-SIO-Delivery-ID"] == "adl_test"
    params = pool.fetchrow.call_args.args[1]
    assert params[0] == expected
    assert "private" not in str(params), (
        "neither a response body nor a destination belongs in history"
    )
    assert params[8] == "lease-one", "completion is fenced by the exact claim"


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("secret URL"),
        httpx.ReadTimeout("secret query token"),
        httpx.InvalidURL("secret malformed destination"),
    ],
)
async def test_network_errors_are_sanitized_and_retryable(error):
    def receiver(request):
        raise error

    pool = AsyncMock()
    pool.fetchrow.return_value = {"delivery_id": "adl_test"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(receiver)) as client:
        outbox = AlertOutbox(pool, client)
        outbox.claim = AsyncMock(return_value=pending_row())
        assert await outbox.dispatch_one("demo", DESTINATION) == "pending"
    assert "secret" not in str(pool.fetchrow.call_args.args[1])


async def test_last_failed_attempt_stops_automatic_retries():
    pool = AsyncMock()
    pool.fetchrow.return_value = {"delivery_id": "adl_test"}
    outbox = AlertOutbox(pool, AsyncMock())
    assert (
        await outbox.finish(
            pending_row(cycle_attempts=MAX_ATTEMPTS),
            status_code=503,
            error="Receiver returned HTTP 503.",
        )
        == "failed"
    )
    assert pool.fetchrow.call_args.args[1][4] is False
    assert retry_delay(1) == 5
    assert retry_delay(1000) == 300


async def test_lost_lease_does_not_report_success():
    pool = AsyncMock()
    pool.fetchrow.return_value = None
    outbox = AlertOutbox(pool, AsyncMock())
    assert await outbox.finish(pending_row(), status_code=204, error=None) == "lease_lost"


async def test_cancellation_leaves_the_claim_for_restart_recovery():
    entered = asyncio.Event()

    async def receiver(request):
        entered.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(receiver)) as client:
        outbox = AlertOutbox(AsyncMock(), client)
        outbox.claim = AsyncMock(return_value=pending_row())
        outbox.finish = AsyncMock()
        worker = asyncio.create_task(outbox.dispatch_one("demo", DESTINATION))
        await entered.wait()
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        outbox.finish.assert_not_awaited()


async def test_disabled_destination_recovers_without_sending():
    client = AsyncMock()
    outbox = AlertOutbox(AsyncMock(), client)
    outbox.recover = AsyncMock()
    outbox.claim = AsyncMock()
    assert await outbox.dispatch("demo", "") == []
    outbox.recover.assert_awaited_once_with("demo", "")
    outbox.claim.assert_not_awaited()
    client.post.assert_not_awaited()


def test_destination_and_public_history_never_expose_url_credentials():
    assert destination_label(DESTINATION) == "https://receiver.invalid"
    assert destination_label("") is None
    public = AlertOutbox.public(pending_row(status="failed"), DESTINATION)
    assert public["destination"] == "https://receiver.invalid"
    assert public["can_retry"] is True
    assert "private" not in json.dumps(public)
    assert "lease_token" not in public and "payload" not in public
    assert not AlertOutbox.public(pending_row(status="failed"), "https://new.invalid")["can_retry"]
    assert not AlertOutbox.public(pending_row(status="delivered"), DESTINATION)["can_retry"]


def test_delivery_identity_is_stable_for_the_same_lifecycle_transition():
    alert = Alert(
        group_key="authored-test", tenant_id="demo", title="An incident", severity=Severity.HIGH
    )
    first = delivery_id(alert, "raised")
    assert (
        delivery_id(alert.model_copy(update={"title": "Updated title", "count": 5}), "raised")
        == first
    )
    assert delivery_id(alert.model_copy(update={"tenant_id": "other"}), "raised") != first
    escalated = alert.model_copy(update={"escalated_ts": utc_now()})
    assert delivery_id(escalated, "escalated") != first
    assert delivery_id(
        escalated.model_copy(update={"escalated_ts": utc_now() + timedelta(seconds=1)}), "escalated"
    ) != delivery_id(escalated, "escalated")


async def test_service_persists_notification_in_the_alert_statement(settings):
    cfg = settings.model_copy(update={"alert_webhook_url": DESTINATION})
    service = AlertsService(cfg)
    service.pool = AsyncMock()
    alert = Alert(
        group_key="authored-test",
        tenant_id=cfg.tenant_id,
        title="Atomic alert",
        severity=Severity.HIGH,
    )
    try:
        await service._persist(alert, notification="raised")
        service.pool.execute.assert_awaited_once()
        sql, params = service.pool.execute.call_args.args
        assert "WITH alert_write AS" in sql and "INSERT INTO alert_deliveries" in sql
        assert json.loads(params[-1])["delivery_id"] == delivery_id(alert, "raised")
        assert params[-2] == DESTINATION
    finally:
        await service.client.aclose()


@pytest.mark.parametrize(
    "roles,status",
    [(("viewer",), 403), (("operator",), 403), (("integrator",), 200), (("admin",), 200)],
)
async def test_retry_requires_integration_permission_and_uses_authenticated_actor(
    settings, roles, status
):
    service = AlertsService(settings)
    service.outbox = AsyncMock()
    service.outbox.retry.return_value = {"delivery_id": "adl_test", "status": "pending"}
    token = DevJwtAuth(settings).issue(subject="actual-operator", roles=roles)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service.app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/alert-deliveries/adl_test/retry",
                headers={"Authorization": f"Bearer {token}"},
                json={"reason": "Receiver repaired", "actor": "forged"},
            )
        assert response.status_code == status, response.text
        if status == 200:
            assert service.outbox.retry.call_args.args[-2:] == (
                "actual-operator",
                "Receiver repaired",
            )
        else:
            service.outbox.retry.assert_not_awaited()
    finally:
        await service.client.aclose()


async def test_history_and_retry_reject_anonymous_and_foreign_tenants(settings):
    service = AlertsService(settings)
    service.outbox = AsyncMock()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service.app), base_url="http://test"
        ) as client:
            assert (await client.get("/alert-deliveries")).status_code == 401
            token = DevJwtAuth(settings).issue(tenant_id="another-tenant", roles=("admin",))
            headers = {"Authorization": f"Bearer {token}"}
            assert (await client.get("/alert-deliveries", headers=headers)).status_code == 403
            assert (
                await client.post(
                    "/alert-deliveries/id/retry",
                    headers=headers,
                    json={"reason": "Receiver repaired"},
                )
            ).status_code == 403
        service.outbox.list.assert_not_awaited()
        service.outbox.retry.assert_not_awaited()
    finally:
        await service.client.aclose()


async def test_no_row_returns_404_without_disclosing_another_tenant():
    pool = AsyncMock()
    pool.fetchrow.return_value = None
    with pytest.raises(HTTPException) as caught:
        await AlertOutbox(pool, AsyncMock()).retry(
            "wrong-tenant", "adl_test", DESTINATION, "operator", "Receiver repaired"
        )
    assert caught.value.status_code == 404


@asynccontextmanager
async def _unit_transaction():
    # These tests isolate route state handling; actual locking is covered in integration.
    yield


async def test_repeated_manual_escalation_does_not_dispatch_again(settings):
    service = AlertsService(settings)
    service._mutation = _unit_transaction
    existing = Alert(
        group_key="authored-test",
        tenant_id=settings.tenant_id,
        title="Already escalated",
        state=AlertState.ESCALATED,
    )
    service._load = AsyncMock(return_value=existing)
    service._persist = AsyncMock()
    token = DevJwtAuth(settings).issue(roles=("operator",))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service.app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/alerts/{existing.alert_id}/escalate",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 200
        service._persist.assert_not_awaited()
    finally:
        await service.client.aclose()


async def test_delivery_routes_pass_authenticated_zone_restrictions(settings):
    service = AlertsService(settings)
    service.outbox = AsyncMock()
    service.outbox.list.return_value = {"deliveries": []}
    service.outbox.retry.return_value = {"status": "pending"}
    token = DevJwtAuth(settings).issue(roles=("integrator",), zones=("public-yard",))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service.app), base_url="http://test"
        ) as client:
            headers = {"Authorization": f"Bearer {token}"}
            assert (await client.get("/alert-deliveries", headers=headers)).status_code == 200
            assert (
                await client.post(
                    "/alert-deliveries/id/retry",
                    headers=headers,
                    json={"reason": "Receiver repaired"},
                )
            ).status_code == 200
        assert service.outbox.list.call_args.kwargs["allowed_zones"] == ("public-yard",)
        assert service.outbox.retry.call_args.kwargs["allowed_zones"] == ("public-yard",)
    finally:
        await service.client.aclose()


async def test_lost_escalation_race_reloads_winner_without_publishing(settings):
    service = AlertsService(settings)
    service._mutation = _unit_transaction
    alert = Alert(tenant_id=settings.tenant_id, title="Incident", group_key="test")
    winner = alert.model_copy(update={"state": AlertState.ACKNOWLEDGED})
    service._load = AsyncMock(side_effect=[alert, winner])
    service._persist = AsyncMock(return_value=False)
    service._emit = AsyncMock()
    token = DevJwtAuth(settings).issue(roles=("operator",))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service.app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/alerts/{alert.alert_id}/escalate", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 200
        assert response.json()["state"] == "acknowledged"
        assert service._persist.call_args.kwargs["expected_state"] == AlertState.OPEN
        service._emit.assert_not_awaited()
        assert service._escalated == 0
    finally:
        await service.client.aclose()
