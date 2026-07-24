"""Integration tests: the real datastores, not the in-memory stand-ins.

Run with live infrastructure:

    just services && just db-init && just neo4j-init && just minio-init
    just test-infra

Everything here is marked ``infra`` and auto-skips otherwise. The point of these tests is to
catch the class of bug the memory adapters cannot: SQL that does not compile, Cypher that
does not parse, a pgvector operator used wrongly, a bucket that exists but rejects writes.
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest

from sio_schemas import (
    Entity,
    EntityState,
    Geo,
    Provenance,
    Relationship,
    RelationshipType,
    new_id,
    utc_now,
)
from sio_schemas.enums import Modality

pytestmark = pytest.mark.infra

TENANT = "itest"


@pytest.fixture
def cfg():  # type: ignore[no-untyped-def]
    from sio_core.config import Settings

    return Settings()


@pytest.fixture
async def pool(cfg):  # type: ignore[no-untyped-def]
    from sio_core.stores.pg import PgPool

    pool = PgPool(cfg.pg_dsn, min_size=1, max_size=3)
    if not await pool.ping():
        pytest.skip("postgres not reachable")
    yield pool
    await pool.close()


def an_entity(entity_id: str | None = None, *, entity_type: str = "truck") -> Entity:
    now = utc_now()
    return Entity(
        entity_id=entity_id or new_id("ent"),
        tenant_id=TENANT,
        type=entity_type,
        label="Truck ABC-123",
        state=EntityState(geo=Geo(lat=37.7749, lon=-122.4194), zone_id="yard", h3_cell="8a2830"),
        provenance=[Provenance(source_id="cam-gate-a", modality=Modality.VIDEO, ts=now)],
        first_seen=now,
        last_seen=now,
        attributes={"plate": "ABC-123", "colour": "red"},
    )


# ------------------------------------------------------------------------------ postgres
async def test_postgres_has_the_expected_schema(pool) -> None:  # type: ignore[no-untyped-def]
    extensions = await pool.extensions()
    assert "postgis" in extensions, "PostGIS is what makes the spatial engine possible"
    assert "vector" in extensions, "pgvector backs semantic search"

    rows = await pool.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )
    tables = {row["table_name"] for row in rows}
    for required in ("entities", "relationships", "events", "embeddings", "audit_log", "zones"):
        assert required in tables, f"missing table {required} — run just db-init"


async def test_audit_log_rejects_mutation(pool) -> None:  # type: ignore[no-untyped-def]
    """The immutable audit trail is a governance requirement, so prove the DB enforces it."""
    from sio_core.errors import StoreError

    audit_id = new_id("aud")
    await pool.execute(
        "INSERT INTO audit_log (tenant_id, audit_id, actor, action) VALUES (%s, %s, %s, %s)",
        (TENANT, audit_id, "itest", "test.write"),
    )
    with pytest.raises(StoreError, match="append-only"):
        await pool.execute(
            "UPDATE audit_log SET actor = 'forged' WHERE tenant_id = %s AND audit_id = %s",
            (TENANT, audit_id),
        )
    with pytest.raises(StoreError, match="append-only"):
        await pool.execute(
            "DELETE FROM audit_log WHERE tenant_id = %s AND audit_id = %s", (TENANT, audit_id)
        )


async def test_events_are_append_only(pool) -> None:  # type: ignore[no-untyped-def]
    from sio_core.errors import StoreError

    event_id = new_id("evt")
    await pool.execute(
        "INSERT INTO events (tenant_id, event_id, type, ts) VALUES (%s, %s, %s, now())",
        (TENANT, event_id, "fire_detected"),
    )
    with pytest.raises(StoreError, match="append-only"):
        await pool.execute(
            "UPDATE events SET severity = 'info' WHERE tenant_id = %s AND event_id = %s",
            (TENANT, event_id),
        )


async def test_postgis_spatial_predicates_work(pool) -> None:  # type: ignore[no-untyped-def]
    """'Trucks within 500 m' must be a real spatial query, not a python loop."""
    zone_id = f"zone_{new_id('z')}"
    await pool.execute(
        """
        INSERT INTO zones (tenant_id, zone_id, name, kind, geom)
        VALUES (%s, %s, 'Test yard', 'area',
                ST_SetSRID(ST_MakePolygon(ST_GeomFromText(
                    'LINESTRING(-122.4200 37.7745, -122.4180 37.7745,
                                -122.4180 37.7755, -122.4200 37.7755, -122.4200 37.7745)'
                )), 4326)::geography)
        """,
        (TENANT, zone_id),
    )
    inside = await pool.fetchval(
        """
        SELECT ST_Contains(
            geom::geometry,
            ST_SetSRID(ST_MakePoint(-122.4194, 37.7749), 4326)
        ) FROM zones WHERE tenant_id = %s AND zone_id = %s
        """,
        (TENANT, zone_id),
    )
    assert inside is True

    distance = await pool.fetchval(
        """
        SELECT ST_Distance(
            ST_SetSRID(ST_MakePoint(-122.4194, 37.7749), 4326)::geography,
            ST_SetSRID(ST_MakePoint(-122.4194, 37.7758), 4326)::geography
        )
        """
    )
    assert 95 < float(distance) < 105, "≈100 m apart in latitude"


# ------------------------------------------------------------------------------ pgvector
async def test_pgvector_similarity_search(pool, cfg) -> None:  # type: ignore[no-untyped-def]
    from sio_core.stores.vectors import DEFAULT_DIM, PgVectorStore

    store = PgVectorStore(pool)
    collection = f"itest_{new_id('c')}"

    def vec(index: int) -> list[float]:
        v = [0.0] * DEFAULT_DIM
        v[index] = 1.0
        return v

    await store.upsert(collection, "a", vec(0), tenant_id=TENANT, metadata={"cam": "gate"})
    await store.upsert(collection, "b", vec(1), tenant_id=TENANT, metadata={"cam": "dock"})

    hits = await store.search(collection, vec(0), tenant_id=TENANT, limit=2)
    assert [item for item, _, _ in hits] == ["a", "b"], "nearest first"
    assert hits[0][1] > 0.99
    assert hits[0][2]["cam"] == "gate", "metadata must round-trip through jsonb"

    filtered = await store.search(collection, vec(0), tenant_id=TENANT, filters={"cam": "dock"})
    assert [item for item, _, _ in filtered] == ["b"]

    assert await store.count(collection, tenant_id=TENANT) == 2
    assert await store.search(collection, vec(0), tenant_id="other-tenant") == []

    stored = await store.get(collection, "a", tenant_id=TENANT)
    assert stored is not None and len(stored[0]) == DEFAULT_DIM

    await store.delete(collection, "a", tenant_id=TENANT)
    assert await store.count(collection, tenant_id=TENANT) == 1


async def test_pgvector_upsert_is_idempotent(pool) -> None:  # type: ignore[no-untyped-def]
    """At-least-once delivery means the same frame may be embedded twice."""
    from sio_core.stores.vectors import DEFAULT_DIM, PgVectorStore

    store = PgVectorStore(pool)
    collection = f"itest_{new_id('c')}"
    vector = [0.5] * DEFAULT_DIM
    await store.upsert(collection, "same", vector, tenant_id=TENANT)
    await store.upsert(collection, "same", vector, tenant_id=TENANT, metadata={"second": True})
    assert await store.count(collection, tenant_id=TENANT) == 1
    stored = await store.get(collection, "same", tenant_id=TENANT)
    assert stored is not None and stored[1] == {"second": True}


# --------------------------------------------------------------- graph: both backends
@pytest.fixture(params=["neo4j", "postgres"])
async def graph(request, cfg, pool):  # type: ignore[no-untyped-def]
    """Yield each real graph adapter in turn — the contract must hold for both."""
    if request.param == "neo4j":
        from sio_core.stores.graph_neo4j import Neo4jGraphStore

        store = Neo4jGraphStore(
            cfg.neo4j_uri, cfg.neo4j_user, cfg.neo4j_password, cfg.neo4j_database
        )
        if not await store.ping():
            await store.close()
            pytest.skip("neo4j not reachable (run just services && just neo4j-init)")
        yield store
        # Leave the database as we found it: these tests use a dedicated tenant.
        await store.raw_query.__self__._run(
            "MATCH (e:Entity {tenant_id: $tenant_id}) DETACH DELETE e", {"tenant_id": TENANT}
        )
        await store.close()
    else:
        from sio_core.stores.graph_pg import PostgresGraphStore

        store = PostgresGraphStore(pool)
        yield store
        await pool.execute("DELETE FROM relationships WHERE tenant_id = %s", (TENANT,))
        await pool.execute("DELETE FROM entities WHERE tenant_id = %s", (TENANT,))


async def test_graph_entity_round_trip(graph) -> None:  # type: ignore[no-untyped-def]
    entity = an_entity()
    await graph.upsert_entity(entity)

    found = await graph.get_entity(entity.entity_id, tenant_id=TENANT)
    assert found is not None
    assert found.label == "Truck ABC-123"
    assert found.attributes["plate"] == "ABC-123", "nested attributes must survive storage"
    assert found.state.geo is not None
    assert found.state.geo.lat == pytest.approx(37.7749, abs=1e-6)
    assert found.provenance and found.provenance[0].source_id == "cam-gate-a"


async def test_graph_upsert_extends_lifetime(graph) -> None:  # type: ignore[no-untyped-def]
    entity = an_entity()
    await graph.upsert_entity(entity)
    later = entity.model_copy(
        update={"last_seen": entity.last_seen + timedelta(minutes=20), "label": None}
    )
    await graph.upsert_entity(later)

    found = await graph.get_entity(entity.entity_id, tenant_id=TENANT)
    assert found is not None
    assert found.last_seen > entity.last_seen, "last_seen advances"
    assert found.first_seen == entity.first_seen, "first_seen never moves forward"


async def test_graph_lifetime_merge_survives_a_replayed_first_seen(graph) -> None:  # type: ignore[no-untyped-def]
    """The bug this guards against made every entity's dwell time read as zero.

    Both real adapters store the entity as JSON *and* as indexed columns. The columns were merged
    with LEAST/GREATEST but the JSON was replaced wholesale, so readers — which deserialise the JSON —
    saw the newest producer's timestamps. Dwell time (UC1) was therefore always 0.
    """
    entity = an_entity()
    original_first_seen = entity.first_seen
    await graph.upsert_entity(entity)

    # A later message that wrongly claims the entity is brand new, as the simulator's periodic
    # ground-truth publish does.
    twenty_minutes_later = utc_now() + timedelta(minutes=20)
    await graph.upsert_entity(
        entity.model_copy(
            update={"first_seen": twenty_minutes_later, "last_seen": twenty_minutes_later}
        )
    )

    found = await graph.get_entity(entity.entity_id, tenant_id=TENANT)
    assert found is not None
    assert found.first_seen == original_first_seen, "the deserialised payload must show the merge"
    assert found.dwell_s() > 60, f"dwell time collapsed to {found.dwell_s()}s"


async def test_graph_find_entities_filters(graph) -> None:  # type: ignore[no-untyped-def]
    await graph.upsert_entities([an_entity(), an_entity(), an_entity(entity_type="person")])
    assert len(await graph.find_entities(tenant_id=TENANT)) >= 3
    trucks = await graph.find_entities(tenant_id=TENANT, entity_type="truck")
    assert len(trucks) == 2
    assert len(await graph.find_entities(tenant_id=TENANT, zone_id="yard")) >= 2
    assert len(await graph.find_entities(tenant_id=TENANT, limit=1)) == 1


async def test_graph_bitemporal_traversal(graph) -> None:  # type: ignore[no-untyped-def]
    """The same bitemporal semantics the memory adapter has, on real storage."""
    truck = an_entity()
    camera = Entity(entity_id=new_id("cam"), tenant_id=TENANT, type="camera", is_static=True)
    await graph.upsert_entities([truck, camera])

    t0 = utc_now()
    rel = Relationship(
        tenant_id=TENANT,
        **{"from": truck.entity_id, "to": camera.entity_id},
        type=RelationshipType.SEEN_BY,
        ts_valid_from=t0,
    )
    await graph.upsert_relationship(rel)

    now_neighbours = await graph.neighbors(truck.entity_id, tenant_id=TENANT)
    assert [e.entity_id for _, e in now_neighbours] == [camera.entity_id]

    await graph.close_relationship(rel.id, tenant_id=TENANT, ts=t0 + timedelta(minutes=5))

    assert (
        len(await graph.neighbors(truck.entity_id, tenant_id=TENANT, at=t0 + timedelta(minutes=1)))
        == 1
    ), "the closed edge must still be visible in the past"
    assert (
        await graph.neighbors(truck.entity_id, tenant_id=TENANT, at=t0 + timedelta(minutes=10))
        == []
    ), "and invisible after it closed"

    counts = await graph.counts(tenant_id=TENANT)
    assert counts["relationships"] >= 1
    assert counts["open_relationships"] == 0, "closing an edge must not delete it"


async def test_graph_path_between(graph) -> None:  # type: ignore[no-untyped-def]
    truck = an_entity()
    camera = Entity(entity_id=new_id("cam"), tenant_id=TENANT, type="camera")
    zone = Entity(entity_id=new_id("zn"), tenant_id=TENANT, type="zone")
    await graph.upsert_entities([truck, camera, zone])
    await graph.upsert_relationship(
        Relationship(
            tenant_id=TENANT,
            **{"from": truck.entity_id, "to": camera.entity_id},
            type=RelationshipType.SEEN_BY,
        )
    )
    await graph.upsert_relationship(
        Relationship(
            tenant_id=TENANT,
            **{"from": camera.entity_id, "to": zone.entity_id},
            type=RelationshipType.COVERS,
        )
    )

    path = await graph.path_between(truck.entity_id, zone.entity_id, tenant_id=TENANT)
    assert len(path) == 2
    assert {str(r.type) for r in path} == {"seen_by", "covers"}


async def test_graph_snapshot_at(graph) -> None:  # type: ignore[no-untyped-def]
    old = an_entity()
    await graph.upsert_entity(old)
    entities, _ = await graph.snapshot_at(utc_now() + timedelta(seconds=1), tenant_id=TENANT)
    assert any(e.entity_id == old.entity_id for e in entities)
    past, _ = await graph.snapshot_at(old.first_seen - timedelta(hours=1), tenant_id=TENANT)
    assert not any(e.entity_id == old.entity_id for e in past)


async def test_graph_tenant_isolation(graph) -> None:  # type: ignore[no-untyped-def]
    entity = an_entity()
    await graph.upsert_entity(entity)
    assert await graph.get_entity(entity.entity_id, tenant_id="someone-else") is None
    assert await graph.find_entities(tenant_id="someone-else") == []


async def test_graph_raw_query_refuses_writes(graph) -> None:  # type: ignore[no-untyped-def]
    """The copilot's traversal tool must not be able to mutate the world model."""
    from sio_core.errors import StoreError

    with pytest.raises(StoreError, match="read-only"):
        await graph.raw_query("MATCH (e:Entity) DETACH DELETE e", tenant_id=TENANT)


async def test_graph_raw_query_reads(graph, cfg) -> None:  # type: ignore[no-untyped-def]
    entity = an_entity()
    await graph.upsert_entity(entity)
    if cfg.graph_backend == "postgres" or type(graph).__name__ == "PostgresGraphStore":
        rows = await graph.raw_query(
            "SELECT entity_id, type FROM entities WHERE tenant_id = %(tenant_id)s LIMIT 5",
            {"tenant_id": TENANT},
            tenant_id=TENANT,
        )
    else:
        rows = await graph.raw_query(
            "MATCH (e:Entity {tenant_id: $tenant_id}) RETURN e.entity_id AS entity_id LIMIT 5",
            tenant_id=TENANT,
        )
    assert rows, "a read-only query must return rows"


# --------------------------------------------------------------------------------- minio
async def test_minio_round_trip(cfg) -> None:  # type: ignore[no-untyped-def]
    from sio_core.stores.blob import MinioBlobStore

    if os.environ.get("SIO_BLOB_BACKEND", cfg.blob_backend) != "minio":
        pytest.skip("blob backend is not minio")
    store = MinioBlobStore(
        cfg.minio_endpoint,
        cfg.minio_access_key,
        cfg.minio_secret_key,
        cfg.minio_bucket,
        secure=cfg.minio_secure,
    )
    if not await store.ping():
        pytest.skip("minio not reachable or bucket missing (run just minio-init)")

    key = f"itest/{new_id('blb')}.jpg"
    payload = b"\xff\xd8" + b"frame-bytes" * 10
    await store.put(key, payload, content_type="image/jpeg", metadata={"camera": "gate-a"})
    assert await store.exists(key)
    assert await store.get(key) == payload
    assert key in await store.list("itest/")
    assert store.url_for(key).endswith(key)
    await store.delete(key)
    assert not await store.exists(key)
    await store.close()


async def test_minio_bucket_exists(cfg) -> None:  # type: ignore[no-untyped-def]
    """Tier 1 regression guard: the bucket is referenced everywhere and created by one script."""
    from sio_core.stores.blob import MinioBlobStore

    store = MinioBlobStore(
        cfg.minio_endpoint,
        cfg.minio_access_key,
        cfg.minio_secret_key,
        cfg.minio_bucket,
        secure=cfg.minio_secure,
    )
    assert await store.ping(), (
        f"bucket {cfg.minio_bucket!r} is missing — scripts/init_minio.py must run during setup"
    )
    await store.close()
