"""Alert concurrency/replay and paginated history against disposable Postgres schemas."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from sio_alerts.outbox import AlertOutbox
from sio_alerts.service import AlertsService

from sio_core import PgPool
from sio_core.authn import DevJwtAuth
from sio_core.config import Settings
from sio_schemas import Alert, AlertState, BusMessage, Event, Topic

pytestmark = pytest.mark.infra
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
async def database(tmp_path):
    cfg = Settings(
        data_dir=tmp_path,
        tenant_id="consistency-test",
        alert_webhook_url="https://receiver.invalid/",
    )
    schema = f"alert_consistency_{uuid4().hex}"
    control = await psycopg.AsyncConnection.connect(cfg.pg_dsn, autocommit=True)
    pool = PgPool(
        make_conninfo(cfg.pg_dsn, options=f"-c search_path={schema},public"), min_size=2, max_size=6
    )
    services = []
    try:
        await control.execute(f"CREATE SCHEMA {schema}")
        await pool.open()
        await pool.execute("CREATE TABLE alerts (LIKE public.alerts INCLUDING ALL)")
        for name in ("009_alert_deliveries.sql", "010_alert_consistency.sql"):
            await pool.execute((ROOT / "infra/postgres" / name).read_text())

        def make():
            service = AlertsService(cfg)
            service.pool = pool
            service.outbox = AlertOutbox(pool, service.client)
            service.publish = AsyncMock()
            services.append(service)
            return service

        yield cfg, pool, make
    finally:
        for service in services:
            await service.client.aclose()
        await pool.close()
        await control.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await control.close()


def trigger(tenant):
    event = Event(
        tenant_id=tenant, type="fire_detected", severity="high", zone_id="yard", confidence=0.9
    )
    return event, BusMessage.of(Topic.EVENTS, event)


async def test_replay_after_restart_and_concurrent_consumers_count_once(database):
    cfg, pool, make = database
    first, second = make(), make()
    event, message = trigger(cfg.tenant_id)
    await asyncio.gather(
        first.on_message(message, AsyncMock()), second.on_message(message, AsyncMock())
    )
    await make().on_message(message, AsyncMock())
    rows = await pool.fetch("SELECT payload FROM alerts")
    assert len(rows) == 1
    assert rows[0]["payload"]["count"] == 1
    assert rows[0]["payload"]["event_ids"] == [event.event_id]
    assert await pool.fetchval("SELECT count(*) FROM alert_consumed_events") == 1
    assert await pool.fetchval("SELECT count(*) FROM alert_deliveries") == 1


async def test_receipt_and_alert_rollback_together_then_retry(database):
    cfg, pool, make = database
    service = make()
    _, message = trigger(cfg.tenant_id)
    await pool.execute("""CREATE FUNCTION fail_alert() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'authored failure'; END $$;
        CREATE TRIGGER fail_alert BEFORE INSERT ON alerts FOR EACH ROW EXECUTE FUNCTION fail_alert();""")
    with pytest.raises(Exception, match="authored failure"):
        await service.on_message(message, AsyncMock())
    assert await pool.fetchval("SELECT count(*) FROM alert_consumed_events") == 0
    await pool.execute("DROP TRIGGER fail_alert ON alerts")
    await service.on_message(message, AsyncMock())
    assert await pool.fetchval("SELECT count(*) FROM alerts") == 1


async def test_concurrent_fold_and_resolution_preserve_terminal_state(database):
    cfg, pool, make = database
    consumer, http = make(), make()
    _, message = trigger(cfg.tenant_id)
    await consumer.on_message(message, AsyncMock())
    identifier = await pool.fetchval("SELECT alert_id FROM alerts")
    entered, release = asyncio.Event(), asyncio.Event()
    original = consumer._fold

    async def delayed(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    consumer._fold = delayed
    _, next_message = trigger(cfg.tenant_id)
    folding = asyncio.create_task(consumer.on_message(next_message, AsyncMock()))
    await asyncio.wait_for(entered.wait(), 2)
    token = DevJwtAuth(cfg).issue(subject="resolver", roles=("operator",), zones=("yard",))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=http.app), base_url="http://test"
    ) as client:
        resolving = asyncio.create_task(
            client.post(
                f"/alerts/{identifier}/resolve",
                headers={"Authorization": f"Bearer {token}"},
                json={"resolved_by": "forged"},
            )
        )
        await asyncio.sleep(0.05)
        assert not resolving.done()
        release.set()
        await folding
        result = await resolving
        assert result.status_code == 200, result.text
    saved = Alert.model_validate(
        await pool.fetchval("SELECT payload FROM alerts WHERE alert_id = %s", (identifier,))
    )
    assert saved.state == AlertState.RESOLVED and saved.count == 2
    assert "resolver" in str(saved.explanation.notes)
    # A stale event retry must not resurrect this resolved alert.
    await make().on_message(next_message, AsyncMock())
    assert (
        await pool.fetchval("SELECT state FROM alerts WHERE alert_id = %s", (identifier,))
        == "resolved"
    )


async def test_delivery_and_history_keyset_pages_have_no_gaps(database):
    cfg, pool, make = database
    service = make()
    for n in range(5):
        alert = Alert(
            tenant_id=cfg.tenant_id, title=f"Incident {n}", group_key=str(n), zone_id="yard"
        )
        await service._persist(alert, notification="raised")
    # Force timestamp ties, which require delivery_id as the stable tiebreaker.
    await pool.execute("UPDATE alert_deliveries SET created_at = '2026-01-01T00:00:00Z'")
    identifiers, cursor = [], None
    while True:
        page = await service.outbox.list(
            cfg.tenant_id, cfg.alert_webhook_url, limit=2, cursor=cursor, allowed_zones=("yard",)
        )
        identifiers.extend(row["delivery_id"] for row in page["deliveries"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(identifiers) == len(set(identifiers)) == 5
    await pool.execute(
        "INSERT INTO alert_delivery_history (tenant_id, delivery_id, kind) SELECT %s, %s, 'queued' FROM generate_series(1, 45)",
        (cfg.tenant_id, identifiers[0]),
    )
    page = await service.outbox.list(
        cfg.tenant_id, cfg.alert_webhook_url, limit=10, allowed_zones=("yard",)
    )
    delivery = next(row for row in page["deliveries"] if row["delivery_id"] == identifiers[0])
    history_ids = [row["history_id"] for row in delivery["history"]]
    cursor = delivery["history_next_cursor"]
    while cursor:
        history = await service.outbox.history(
            cfg.tenant_id, identifiers[0], limit=20, cursor=cursor, allowed_zones=("yard",)
        )
        history_ids.extend(row["history_id"] for row in history["history"])
        cursor = history["next_cursor"]
    assert len(history_ids) == len(set(history_ids)) == 46


async def test_decision_sources_filter_before_limit_and_actions_use_verified_actor(database):
    from sio_decision.service import DecisionService

    from sio_schemas import Decision

    cfg, pool, _ = database
    await pool.execute("CREATE TABLE events (LIKE public.events INCLUDING ALL)")
    await pool.execute("CREATE TABLE decisions (LIKE public.decisions INCLUDING ALL)")
    service = DecisionService(cfg)
    service.pool = pool
    service.publish = AsyncMock()
    identifiers = {}
    for zone in ("yard", "private", None):
        source = Event(
            tenant_id=cfg.tenant_id,
            type="fire_detected",
            severity="high",
            zone_id=zone,
            confidence=0.9,
        )
        await pool.execute(
            "INSERT INTO events (tenant_id,event_id,type,ts,zone_id,payload) VALUES (%s,%s,%s,%s,%s,%s::jsonb)",
            (
                source.tenant_id,
                source.event_id,
                str(source.type),
                source.ts,
                zone,
                source.to_json(),
            ),
        )
        decision = Decision(tenant_id=cfg.tenant_id, trigger_event=source.event_id)
        await service._persist(decision)
        identifiers[zone] = decision.decision_id
    token = DevJwtAuth(cfg).issue(
        subject="real-commander", roles=("commander",), clearance=3, zones=("yard",)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=service.app), base_url="http://test"
    ) as client:
        headers = {"Authorization": f"Bearer {token}"}
        page = await client.get("/decisions?limit=1", headers=headers)
        assert page.status_code == 200, page.text
        assert [item["decision_id"] for item in page.json()["decisions"]] == [identifiers["yard"]]
        for hidden in ("private", None):
            assert (
                await client.get(f"/decisions/{identifiers[hidden]}", headers=headers)
            ).status_code == 404
            assert (
                await client.post(
                    f"/decisions/{identifiers[hidden]}/approve",
                    headers=headers,
                    json={"approved_by": "forged"},
                )
            ).status_code == 404
            assert (
                await client.post(
                    f"/decisions/{identifiers[hidden]}/reject", headers=headers, json={}
                )
            ).status_code == 404
        accepted = await client.post(
            f"/decisions/{identifiers['yard']}/approve",
            headers=headers,
            json={"approved_by": "forged"},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["approved_by"] == "real-commander"
