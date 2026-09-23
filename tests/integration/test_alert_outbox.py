"""Real Postgres transactions/claims in disposable schemas; HTTP is always mocked.

Run only against the coordinator's isolated test database with SIO_TEST_INFRA=1.
Each schema is random and is dropped in cleanup. Existing application rows are
never read or modified (only the public alerts table definition is copied).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi import HTTPException
from psycopg.conninfo import make_conninfo
from sio_alerts.outbox import AlertOutbox, delivery_id
from sio_alerts.service import AlertsService

from sio_core import PgPool
from sio_core.config import Settings
from sio_core.errors import StoreError
from sio_schemas import Alert, AlertState, Severity, utc_now

pytestmark = pytest.mark.infra
URL = "https://receiver.invalid/private?token=never-print"
MIGRATION = Path(__file__).resolve().parents[2] / "infra/postgres/009_alert_deliveries.sql"


@pytest.fixture
async def database(tmp_path):
    cfg = Settings(data_dir=tmp_path, tenant_id="outbox-test", alert_webhook_url=URL)
    schema = f"outbox_test_{uuid4().hex}"
    control = await psycopg.AsyncConnection.connect(cfg.pg_dsn, autocommit=True)
    pool = PgPool(
        make_conninfo(cfg.pg_dsn, options=f"-c search_path={schema},public"), min_size=2, max_size=4
    )
    try:
        await control.execute(f"CREATE SCHEMA {schema}")
        await pool.open()
        await pool.execute("CREATE TABLE alerts (LIKE public.alerts INCLUDING ALL)")
        await pool.execute(await asyncio.to_thread(MIGRATION.read_text))
        yield cfg, pool
    finally:
        await pool.close()
        await control.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await control.close()


async def persist(cfg, pool, *, alert=None):
    service = AlertsService(cfg)
    service.pool = pool
    alert = alert or Alert(
        group_key="authored-test",
        tenant_id=cfg.tenant_id,
        title="Test incident",
        severity=Severity.HIGH,
    )
    try:
        await service._persist(alert, notification="raised")
    finally:
        await service.client.aclose()
    return alert


async def test_alert_and_outbox_are_atomic_and_duplicate_enqueue_keeps_snapshot(database):
    cfg, pool = database
    alert = await persist(cfg, pool)
    identifier = delivery_id(alert, "raised")
    await persist(
        cfg.model_copy(update={"alert_webhook_url": "https://changed.invalid/"}),
        pool,
        alert=alert.model_copy(update={"title": "Later title"}),
    )
    rows = await pool.fetch("SELECT * FROM alert_deliveries")
    assert len(rows) == 1
    assert rows[0]["delivery_id"] == identifier
    assert rows[0]["destination_url"] == URL
    assert rows[0]["payload"]["title"] == "Test incident"
    assert await pool.fetchval("SELECT count(*) FROM alert_delivery_history") == 1

    # A real outbox INSERT failure must roll back the alert mutation as well.
    await pool.execute("""
        CREATE FUNCTION refuse_outbox() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'authored outbox failure'; END $$;
        CREATE TRIGGER refuse_outbox BEFORE INSERT ON alert_deliveries
            FOR EACH ROW EXECUTE FUNCTION refuse_outbox();
    """)
    rejected = Alert(
        group_key="authored-test",
        tenant_id=cfg.tenant_id,
        title="Must roll back",
        severity=Severity.HIGH,
    )
    with pytest.raises(StoreError, match="authored outbox failure"):
        await persist(cfg, pool, alert=rejected)
    assert (
        await pool.fetchrow("SELECT alert_id FROM alerts WHERE alert_id = %s", (rejected.alert_id,))
        is None
    )
    assert await pool.fetchval("SELECT count(*) FROM alert_deliveries") == 1


async def test_concurrent_workers_claim_once_and_success_cannot_be_retried(database):
    cfg, pool = database
    alert = await persist(cfg, pool)
    requests = []

    def receiver(request):
        requests.append(request)
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(receiver)) as client:
        first, second = AlertOutbox(pool, client), AlertOutbox(pool, client)
        claims = await asyncio.gather(
            first.claim(cfg.tenant_id, URL), second.claim(cfg.tenant_id, URL)
        )
        claimed = [row for row in claims if row]
        assert len(claimed) == 1
        row = claimed[0]
        assert row["attempts"] == 1
        assert (
            await first.finish({**row, "lease_token": "stale-worker"}, status_code=204, error=None)
            == "lease_lost"
        )
        assert await first.finish(row, status_code=204, error=None) == "delivered"
        assert await second.dispatch_one(cfg.tenant_id, URL) is None
        assert requests == []
        with pytest.raises(HTTPException) as caught:
            await first.retry(
                cfg.tenant_id, delivery_id(alert, "raised"), URL, "operator", "Trying again"
            )
        assert caught.value.status_code == 409
        listing = await first.list(cfg.tenant_id, URL)
        assert listing["deliveries"][0]["status"] == "delivered"
        assert listing["deliveries"][0]["history"][0]["kind"] == "delivered"


async def test_restart_recovers_expired_claim_with_same_idempotency_key(database):
    cfg, pool = database
    alert = await persist(cfg, pool)
    identifier = delivery_id(alert, "raised")
    requests = []

    def receiver(request):
        requests.append(request)
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(receiver)) as client:
        interrupted = AlertOutbox(pool, client)
        claim = await interrupted.claim(cfg.tenant_id, URL)
        assert claim is not None
        restarted = AlertOutbox(pool, client)
        await restarted.recover(cfg.tenant_id, URL)
        assert await restarted.claim(cfg.tenant_id, URL) is None, (
            "an active lease is not recoverable"
        )
        await pool.execute("UPDATE alert_deliveries SET lease_until = now() - interval '1 second'")
        await restarted.recover(cfg.tenant_id, URL)
        assert await restarted.dispatch_one(cfg.tenant_id, URL) == "delivered"
        assert len(requests) == 1
        assert requests[0].headers["Idempotency-Key"] == identifier
        assert json.loads(requests[0].content)["delivery_id"] == identifier
        row = (await restarted.list(cfg.tenant_id, URL))["deliveries"][0]
        assert row["attempts"] == 2
        assert "recovered" in [item["kind"] for item in row["history"]]
        assert (
            await interrupted.finish(claim, status_code=500, error="Receiver returned HTTP 500.")
            == "lease_lost"
        )


async def test_failure_backoff_exhaustion_and_manual_retry_are_persisted(database):
    cfg, pool = database
    alert = await persist(cfg, pool)
    identifier = delivery_id(alert, "raised")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503, text="secret response"))
    ) as client:
        outbox = AlertOutbox(pool, client)
        assert await outbox.dispatch_one(cfg.tenant_id, URL) == "pending"
        row = await pool.fetchrow(
            "SELECT *, next_attempt_at > now() AS delayed FROM alert_deliveries"
        )
        assert row["delayed"] is True
        assert await outbox.claim(cfg.tenant_id, URL) is None
        for attempt in range(2, 6):
            await pool.execute(
                "UPDATE alert_deliveries SET next_attempt_at = now() - interval '1 second'"
            )
            assert await outbox.dispatch_one(cfg.tenant_id, URL) == (
                "failed" if attempt == 5 else "pending"
            )
        assert await outbox.claim(cfg.tenant_id, URL) is None
        result = await outbox.retry(
            cfg.tenant_id, identifier, URL, "verified-integrator", "Receiver service repaired"
        )
        assert (
            result["status"] == "pending"
            and result["attempts"] == 5
            and result["cycle_attempts"] == 0
        )
        with pytest.raises(HTTPException) as caught:
            await outbox.retry(
                cfg.tenant_id, identifier, URL, "verified-integrator", "Accidental double click"
            )
        assert caught.value.status_code == 409
        history = await pool.fetch(
            "SELECT * FROM alert_delivery_history WHERE kind = 'manual_retry'"
        )
        assert len(history) == 1 and history[0]["actor"] == "verified-integrator"
        assert history[0]["reason"] == "Receiver service repaired"
        listing = await outbox.list(cfg.tenant_id, URL)
        assert "never-print" not in json.dumps(listing, default=str)
        assert "secret response" not in json.dumps(listing, default=str)


async def test_changed_destination_blocks_without_retargeting_and_tenant_cannot_retry(database):
    cfg, pool = database
    alert = await persist(cfg, pool)
    identifier = delivery_id(alert, "raised")
    requests = []

    def receiver(request):
        requests.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(receiver)) as client:
        outbox = AlertOutbox(pool, client)
        await outbox.dispatch(cfg.tenant_id, "https://changed.invalid")
        assert requests == []
        row = (await outbox.list(cfg.tenant_id, "https://changed.invalid"))["deliveries"][0]
        assert row["status"] == "blocked" and row["can_retry"] is False
        with pytest.raises(HTTPException) as caught:
            await outbox.retry(
                cfg.tenant_id,
                identifier,
                "https://changed.invalid",
                "integrator",
                "Try changed destination",
            )
        assert caught.value.status_code == 409
        with pytest.raises(HTTPException) as caught:
            await outbox.retry("other-tenant", identifier, URL, "integrator", "Try another tenant")
        assert caught.value.status_code == 404
        assert (await outbox.list("other-tenant", URL))["deliveries"] == []
        # Restoring the original destination still needs an explicit, audited retry.
        await outbox.recover(cfg.tenant_id, URL)
        assert await outbox.claim(cfg.tenant_id, URL) is None
        await outbox.retry(
            cfg.tenant_id, identifier, URL, "integrator", "Original endpoint restored"
        )
        assert await outbox.dispatch_one(cfg.tenant_id, URL) == "delivered"
        assert str(requests[0].url) == URL


async def test_concurrent_escalation_commits_only_one_notification(database):
    cfg, pool = database
    alert = await persist(cfg, pool)
    service = AlertsService(cfg)
    service.pool = pool
    first = alert.model_copy(update={"state": AlertState.ESCALATED, "escalated_ts": utc_now()})
    second = alert.model_copy(update={"state": AlertState.ESCALATED, "escalated_ts": utc_now()})
    try:
        outcomes = await asyncio.gather(
            service._persist(first, "escalated", expected_state=AlertState.OPEN),
            service._persist(second, "escalated", expected_state=AlertState.OPEN),
        )
        assert sorted(outcomes) == [False, True]
        rows = await pool.fetch("SELECT * FROM alert_deliveries WHERE action = 'escalated'")
        assert len(rows) == 1
        winner = first if outcomes[0] else second
        assert rows[0]["delivery_id"] == delivery_id(winner, "escalated")
    finally:
        await service.client.aclose()


async def test_delivery_history_and_retry_enforce_snapshot_zone_scope(database):
    cfg, pool = database
    public = Alert(tenant_id=cfg.tenant_id, title="Public yard", group_key="public", zone_id="yard")
    private = Alert(
        tenant_id=cfg.tenant_id, title="Restricted laboratory", group_key="private", zone_id="lab"
    )
    await persist(cfg, pool, alert=public)
    await persist(cfg, pool, alert=private)
    await pool.execute("UPDATE alert_deliveries SET status = 'failed'")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    ) as client:
        outbox = AlertOutbox(pool, client)
        listing = await outbox.list(cfg.tenant_id, URL, allowed_zones=("yard",))
        assert [row["title"] for row in listing["deliveries"]] == ["Public yard"]
        with pytest.raises(HTTPException) as caught:
            await outbox.retry(
                cfg.tenant_id,
                delivery_id(private, "raised"),
                URL,
                "narrow-integrator",
                "Receiver repaired",
                allowed_zones=("yard",),
            )
        assert caught.value.status_code == 404
        assert (
            await pool.fetchval(
                "SELECT count(*) FROM alert_delivery_history WHERE kind = 'manual_retry'"
            )
            == 0
        )
        result = await outbox.retry(
            cfg.tenant_id,
            delivery_id(public, "raised"),
            URL,
            "narrow-integrator",
            "Receiver repaired",
            allowed_zones=("yard",),
        )
        assert result["status"] == "pending"
