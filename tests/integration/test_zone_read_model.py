"""Zone predicates run before counts, LIMIT and historical reconstruction in real SQL."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from sio_api.queries import ReadModel
from sio_api.timeline import TimelineReader

from sio_core import PgPool
from sio_core.config import Settings
from sio_schemas import Entity, EntityState, Event, Geo, utc_now

pytestmark = pytest.mark.infra
TENANT = "scope-fixture"


@pytest.fixture
async def scoped_database(tmp_path):
    cfg = Settings(data_dir=tmp_path)
    schema = f"scope_test_{uuid4().hex}"
    control = await psycopg.AsyncConnection.connect(cfg.pg_dsn, autocommit=True)
    pool = PgPool(
        make_conninfo(cfg.pg_dsn, options=f"-c search_path={schema},public"), min_size=1, max_size=2
    )
    try:
        await control.execute(f"CREATE SCHEMA {schema}")
        await pool.open()
        for table in [
            "entities",
            "entity_states",
            "relationships",
            "events",
            "zones",
            "sources",
            "observations",
            "measurements",
        ]:
            await pool.execute(f"CREATE TABLE {table} (LIKE public.{table} INCLUDING ALL)")
        yield pool
    finally:
        await pool.close()
        await control.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await control.close()


async def entity(pool, identifier, zone, ts, *, tenant=TENANT, static=False):
    row = Entity(
        tenant_id=tenant,
        entity_id=identifier,
        type="vehicle",
        is_static=static,
        first_seen=ts - timedelta(hours=1),
        last_seen=ts,
        state=EntityState(ts=ts, geo=Geo(lat=1, lon=1), zone_id=zone),
    )
    await pool.execute(
        "INSERT INTO entities (tenant_id,entity_id,type,is_static,zone_id,first_seen,last_seen,geom,payload) VALUES (%s,%s,'vehicle',%s,%s,%s,%s,ST_GeogFromText('POINT(1 1)'),%s::jsonb)",
        (tenant, identifier, static, zone, row.first_seen, ts, row.model_dump_json(by_alias=True)),
    )
    return row


async def state(pool, identifier, ts, zone=None):
    await pool.execute(
        "INSERT INTO entity_states (tenant_id,entity_id,ts,zone_id,geom) VALUES (%s,%s,%s,%s,ST_GeogFromText('POINT(1 1)'))",
        (TENANT, identifier, ts, zone),
    )


async def event(pool, identifier, zone, ts, *, tenant=TENANT):
    row = Event(tenant_id=tenant, event_id=identifier, type="entity_appeared", zone_id=zone, ts=ts)
    await pool.execute(
        "INSERT INTO events (tenant_id,event_id,type,zone_id,ts,payload) VALUES (%s,%s,%s,%s,%s,%s::jsonb)",
        (tenant, identifier, str(row.type), zone, ts, row.model_dump_json(by_alias=True)),
    )


async def test_list_detail_count_and_offset_do_not_leak_disallowed_or_unknown_zones(
    scoped_database,
):
    pool = scoped_database
    now = utc_now()
    await entity(pool, "allowed-old", "allowed", now - timedelta(seconds=10))
    await entity(pool, "allowed-new", "allowed", now - timedelta(seconds=5))
    await entity(pool, "private-newest", "other", now)
    await entity(pool, "unknown", None, now)
    await entity(pool, "foreign", "allowed", now, tenant="foreign")
    for ident, zone, offset in [
        ("allowed-event", "allowed", -10),
        ("private-event", "other", 0),
        ("unknown-event", None, 0),
    ]:
        await event(pool, ident, zone, now + timedelta(seconds=offset))
    read = ReadModel(pool)
    scope = {"tenant_id": TENANT, "allowed_zones": ("allowed",)}
    assert [row.entity_id for row in await read.entities(**scope, limit=1)] == ["allowed-new"]
    assert [row.entity_id for row in await read.entities(**scope, limit=1, offset=1)] == [
        "allowed-old"
    ]
    assert await read.entity("private-newest", **scope) is None
    assert await read.entity("unknown", **scope) is None
    assert await read.entity_counts(**scope) == {"vehicle": 2}
    assert [row.event_id for row in await read.events(**scope, limit=1)] == ["allowed-event"]
    stats = await read.stats(**scope)
    assert stats["entities"] == 2 and stats["events"] == 1
    assert stats["latest_entity"] == now - timedelta(seconds=5)
    assert len(await read.entities(tenant_id=TENANT, limit=10)) == 4


async def test_historical_scope_uses_membership_at_the_requested_instant(scoped_database):
    pool = scoped_database
    at = utc_now() - timedelta(minutes=1)
    await entity(pool, "then-allowed", "other", at + timedelta(seconds=20))
    await entity(pool, "then-private", "allowed", at + timedelta(seconds=20))
    for identifier in ["then-allowed", "then-private"]:
        await state(pool, identifier, at - timedelta(seconds=1))
        await state(pool, identifier, at + timedelta(seconds=20))
    for identifier, old, new in [
        ("then-allowed", "allowed", "other"),
        ("then-private", "other", "allowed"),
    ]:
        for zone, start, end in [
            (old, at - timedelta(minutes=2), at + timedelta(seconds=10)),
            (new, at + timedelta(seconds=10), None),
        ]:
            await pool.execute(
                "INSERT INTO relationships (tenant_id,rel_id,from_id,type,to_id,ts_valid_from,ts_valid_to) VALUES (%s,%s,%s,'entered',%s,%s,%s)",
                (TENANT, uuid4().hex, identifier, zone, start, end),
            )
    await event(pool, "visible", "allowed", at)
    await event(pool, "private-older", "other", at - timedelta(hours=1))
    reader = TimelineReader(pool)
    scope = {"tenant_id": TENANT, "allowed_zones": ("allowed",)}
    historical = await reader.world_at(at, **scope, limit=1)
    assert [row.entity_id for row in historical["entities"]] == ["then-allowed"]
    assert historical["entities"][0].state.zone_id == "allowed"
    assert historical["counts"] == {"total": 1, "movers": 1, "static": 0, "in_zones": 1}
    history = await ReadModel(pool).entity_history("then-allowed", **scope)
    assert [row["ts"] for row in history] == [at - timedelta(seconds=1)]
    bounds = await reader.bounds(**scope)
    assert datetime.fromisoformat(bounds["first_event"]) == at
    density = await reader.density(
        **scope, start=at - timedelta(hours=2), end=at + timedelta(minutes=1), buckets=8
    )
    assert density["total"] == 1
    assert [
        row.event_id
        for row in await reader.events_between(
            **scope, start=at - timedelta(hours=2), end=at + timedelta(minutes=1), limit=1
        )
    ] == ["visible"]


async def test_camera_and_observation_counts_derive_authorization_from_registered_source(
    scoped_database,
):
    pool = scoped_database
    now = utc_now()
    for identifier, zone in [("source-a", "allowed"), ("source-b", "other"), ("source-u", None)]:
        await pool.execute(
            "INSERT INTO sources (tenant_id,source_id,kind,modality,zone_id,geom,config) VALUES (%s,%s,'camera','video',%s,ST_GeogFromText('POINT(1 1)'),'{\"password\":\"never-expose\"}')",
            (TENANT, identifier, zone),
        )
        await pool.execute(
            "INSERT INTO observations (tenant_id,observation_id,source_id,modality,ts) VALUES (%s,%s,%s,'video',%s)",
            (TENANT, identifier, identifier, now),
        )
    read = ReadModel(pool)
    scope = {"tenant_id": TENANT, "allowed_zones": ("allowed",)}
    assert (await read.stats(**scope))["observations"] == 1
    cameras = await read.cameras(**scope)
    assert [row["source_id"] for row in cameras] == ["source-a"]
    assert "config" not in cameras[0]
