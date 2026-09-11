"""One operator journey through real components and PostgreSQL, rolled back after the test.

The sensor sample is authored test data. MemoryBus/MemoryGraph replace external transports;
all service handlers, rule matching, SQL persistence, authentication and replay are real.
No camera, gate or drone hardware is contacted or claimed verified by this scenario.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from psycopg.rows import dict_row
from sio_alerts.service import AlertsService
from sio_api.timeline import TimelineReader
from sio_decision.service import DecisionService
from sio_events.service import EventsService
from sio_worldmodel.service import WorldModelService

from sio_core import registry
from sio_core.authn import DevJwtAuth
from sio_core.bus.memory import MemoryBus
from sio_core.config import Settings
from sio_core.stores.pg import PgPool
from sio_schemas import Entity, EntityState, Event, Geo, Modality, Observation, Topic, utc_now

pytestmark = pytest.mark.infra


class TransactionQueries:
    """The same PostgreSQL calls on one rollback-only connection instead of the pooled connection."""

    def __init__(self, connection):
        self.connection = connection

    async def execute(self, sql, params=None):
        cursor = await self.connection.execute(sql, params)
        return cursor.rowcount

    async def fetch(self, sql, params=None):
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(sql, params)
            return await cursor.fetchall()

    async def fetchrow(self, sql, params=None):
        rows = await self.fetch(sql, params)
        return rows[0] if rows else None

    async def fetchval(self, sql, params=None):
        row = await self.fetchrow(sql, params)
        return next(iter(row.values())) if row else None


async def test_sensor_incident_approval_and_historical_replay(tmp_path):
    cfg = Settings(
        data_dir=tmp_path,
        tenant_id=f"acceptance-{uuid4().hex}",
        bus_backend="memory",
        graph_backend="memory",
        vector_backend="memory",
        blob_backend="file",
        embedder="hash",
        llm_provider="scripted",
        auth_mode="dev",
        decision_use_llm=False,
        alert_webhook_url="",
    )
    bus = MemoryBus()
    db = PgPool(cfg.pg_dsn, min_size=1, max_size=1)
    await db.open()
    services = [
        WorldModelService(cfg, bus=bus),
        EventsService(cfg, bus=bus),
        AlertsService(cfg, bus=bus),
        DecisionService(cfg, bus=bus),
    ]
    world, events, alerts, decisions = services
    try:
        async with db._pool.connection() as connection:
            async with connection.transaction(force_rollback=True):
                queries = TransactionQueries(connection)
                for service in services:
                    service.pool = queries
                await queries.execute(
                    "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)",
                    (cfg.tenant_id, "Acceptance scenario (rolled back)"),
                )
                ts = utc_now()
                responder = Entity(
                    tenant_id=cfg.tenant_id,
                    entity_id="responder",
                    type="drone",
                    first_seen=ts - timedelta(seconds=2),
                    last_seen=ts,
                    attributes={"battery_pct": 90},
                    state=EntityState(ts=ts, geo=Geo(lat=37.7749, lon=-122.4194)),
                )
                reading = Observation(
                    tenant_id=cfg.tenant_id,
                    source_id="test-temperature",
                    modality=Modality.IOT,
                    ts=ts,
                    geo=Geo(lat=37.7750, lon=-122.4194),
                    payload={
                        "metric": "temperature_c",
                        "value": 85,
                        "zone_id": "dock_3",
                        "unit": "C",
                        "test_data": True,
                    },
                )
                await bus.publish(str(Topic.ENTITIES), responder, producer="acceptance")
                await bus.publish(str(Topic.RAW_IOT), reading, producer="acceptance")
                assert await world.drain(limit=2, timeout_s=2) == 2
                assert (
                    await queries.fetchval(
                        "SELECT value FROM measurements WHERE tenant_id = %s AND source_id = %s",
                        (cfg.tenant_id, reading.source_id),
                    )
                    == 85
                )
                # The events consumer consumes its own resulting event to persist it.
                assert await events.drain(limit=3, timeout_s=2) == 3
                row = await queries.fetchrow(
                    "SELECT payload FROM events WHERE tenant_id = %s AND type = %s",
                    (cfg.tenant_id, "temperature_spike"),
                )
                assert row is not None
                event = Event.model_validate(row["payload"])
                assert event.source_ids == [reading.source_id]
                assert any(ref.ref == reading.id for ref in event.explanation.evidence)
                assert await alerts.drain(limit=1, timeout_s=2) == 1
                alert = await queries.fetchrow(
                    "SELECT payload FROM alerts WHERE tenant_id = %s",
                    (cfg.tenant_id,),
                )
                assert event.event_id in alert["payload"]["event_ids"]
                # An operator requests a fire-response recommendation for the high temperature incident.
                decision = await decisions.recommend_for(event, "fire", None)
                assert decision is not None and decision.approval == "pending"
                token = DevJwtAuth(cfg).issue(
                    subject="commander-test", roles=("commander",), clearance=2
                )
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=decisions.app),
                    base_url="http://decisions",
                    headers={"Authorization": f"Bearer {token}"},
                ) as client:
                    response = await client.post(
                        f"/decisions/{decision.decision_id}/approve",
                        json={"approved_by": "commander-test", "note": "Inspect sensor evidence"},
                    )
                    assert response.status_code == 200, response.text
                    duplicate = await client.post(
                        f"/decisions/{decision.decision_id}/approve",
                        json={},
                    )
                    assert duplicate.status_code == 409
                stored = await decisions._load(decision.decision_id)
                assert stored.approval == "approved"
                assert stored.approved_by == "commander-test"
                assert stored.executed_ts is None  # Approval is a record, not physical actuation.
                # Move the responder, then read the earlier world from the persisted history.
                later = ts + timedelta(seconds=10)
                moved = responder.model_copy(
                    update={
                        "last_seen": later,
                        "state": EntityState(
                            ts=later,
                            geo=Geo(lat=37.7800, lon=-122.4100),
                        ),
                    }
                )
                await bus.publish(str(Topic.ENTITIES), moved, producer="acceptance")
                assert await world.drain(limit=1, timeout_s=2) == 1
                reader = TimelineReader(queries)
                historical = await reader.world_at(
                    ts + timedelta(seconds=1), tenant_id=cfg.tenant_id
                )
                restored = next(
                    entity for entity in historical["entities"] if entity.entity_id == "responder"
                )
                assert restored.state.geo == responder.state.geo
                assert restored.state.geo != moved.state.geo
                replay_events = await reader.events_between(
                    tenant_id=cfg.tenant_id,
                    start=ts - timedelta(seconds=1),
                    end=later,
                )
                assert [item.event_id for item in replay_events] == [event.event_id]
                assert all(service._counters.get("errors", 0) == 0 for service in services)
    finally:
        for service in reversed(services):
            await service.teardown()
        await registry.close_all()
        await db.close()
