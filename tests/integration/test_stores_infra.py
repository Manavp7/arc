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


# ------------------------------------------------- dual-write consistency (regression)
async def test_a_relationship_lands_in_both_stores(pool, cfg) -> None:  # type: ignore[no-untyped-def]
    """The world model writes edges to the graph *and* to the relational projection.

    Regression: an earlier version wrote only the graph, so 59 SEEN_BY edges existed in Neo4j while
    the API — which reads Postgres — reported zero relationships. Each store looked fine on its own,
    which is exactly why this needs asserting across both.
    """
    from sio_worldmodel.service import WorldModelService

    from sio_core.stores.graph_neo4j import Neo4jGraphStore

    service = WorldModelService.__new__(WorldModelService)
    service.pool = pool
    graph = Neo4jGraphStore(cfg.neo4j_uri, cfg.neo4j_user, cfg.neo4j_password, cfg.neo4j_database)
    if not await graph.ping():
        await graph.close()
        pytest.skip("neo4j not reachable")
    service.graph = graph
    service._relationships_seen = 0

    truck = an_entity()
    camera = Entity(entity_id=new_id("cam"), tenant_id=TENANT, type="camera", is_static=True)
    await graph.upsert_entities([truck, camera])

    relationship = Relationship(
        tenant_id=TENANT,
        **{"from": truck.entity_id, "to": camera.entity_id},
        type=RelationshipType.SEEN_BY,
        confidence=0.9,
    )
    await service._handle_relationship(relationship)

    # In Postgres...
    row = await pool.fetchrow(
        "SELECT type, from_id, to_id FROM relationships WHERE tenant_id = %s AND rel_id = %s",
        (TENANT, relationship.id),
    )
    assert row is not None, "the edge is missing from the relational projection"
    assert row["type"] == "seen_by"

    # ...and in the graph.
    neighbours = await graph.neighbors(truck.entity_id, tenant_id=TENANT)
    assert any(entity.entity_id == camera.entity_id for _rel, entity in neighbours), (
        "the edge is missing from the graph"
    )

    await pool.execute("DELETE FROM relationships WHERE tenant_id = %s", (TENANT,))
    await graph.raw_query.__self__._run(
        "MATCH (e:Entity {tenant_id: $tenant_id}) DETACH DELETE e", {"tenant_id": TENANT}
    )
    await graph.close()


# --------------------------------------------------- spatial SQL (regression)
async def test_every_spatial_query_runs_against_postgis(pool, cfg) -> None:  # type: ignore[no-untyped-def]
    """Execute every spatial query for real.

    Regression: `within_radius` referenced `e.geo` when the column is `e.geom` — the schema field name
    is `geo`, the database column is not. Unit tests cannot catch that (no database) and the service
    started up perfectly happily; the first request returned a 500. SQL is code, and code that never
    runs is code that does not work.

    These assertions are deliberately about *shape* rather than content: the point is that each
    statement parses, binds and executes against the real schema.
    """
    from sio_spatial.queries import SpatialQueries

    queries = SpatialQueries(pool, cfg.tenant_id)
    here = Geo(lat=37.7749, lon=-122.4194)

    assert isinstance(await queries.load_zones(), list)
    assert isinstance(await queries.load_camera_footprints(), list)
    assert isinstance(await queries.within_radius(here, 500.0), list)
    assert isinstance(await queries.within_radius(here, 500.0, entity_type="truck"), list)
    assert isinstance(await queries.within_radius(here, 500.0, active_within_s=None), list)
    assert isinstance(await queries.nearest(here), list)
    assert isinstance(await queries.nearest(here, entity_type="camera", limit=3), list)
    assert isinstance(await queries.zones_at(here), list)
    assert isinstance(await queries.contains("gate_a"), list)
    assert isinstance(await queries.contains("gate_a", active_within_s=None), list)

    coverage = await queries.coverage_of("cam-gate-a")
    assert "zones" in coverage and "found" in coverage
    assert isinstance(await queries.cameras_covering("gate_a"), list)

    blind = await queries.blind_spots()
    assert {"site_m2", "covered_m2", "coverage_fraction", "gaps"} <= set(blind)
    assert 0.0 <= blind["coverage_fraction"] <= 1.0, "a coverage fraction outside [0,1] is nonsense"

    assert isinstance(await queries.h3_density(resolution=12), list)


async def test_postgis_and_shapely_agree_on_zone_membership(pool, cfg) -> None:  # type: ignore[no-untyped-def]
    """The hot path decides membership in shapely; PostGIS answers ad-hoc queries.

    Two implementations of point-in-polygon that quietly disagree is a bug that surfaces as an
    inexplicable event timeline at 3 a.m. rather than as a failing assertion, so it gets asserted here
    — over a grid of points spanning the site, including points on and just outside boundaries.
    """
    from sio_spatial.geometry import ZoneIndex
    from sio_spatial.queries import SpatialQueries

    queries = SpatialQueries(pool, cfg.tenant_id)
    zones = await queries.load_zones()
    if not zones:
        pytest.skip("no zones seeded; run: just seed")
    index = ZoneIndex(zones)

    # A grid across the bounding box of every zone, plus each zone's centroid and corners.
    lons = [zone.polygon.bounds[0] for zone in zones] + [zone.polygon.bounds[2] for zone in zones]
    lats = [zone.polygon.bounds[1] for zone in zones] + [zone.polygon.bounds[3] for zone in zones]
    samples = [zone.centroid for zone in zones]
    for step in range(6):
        fraction = step / 5
        samples.append(
            Geo(
                lat=min(lats) + (max(lats) - min(lats)) * fraction,
                lon=min(lons) + (max(lons) - min(lons)) * fraction,
            )
        )

    # Points lying exactly ON a boundary are excluded, and that exclusion is the interesting part.
    #
    # Two libraries evaluating an exact predicate on floating-point coordinates can always differ for a
    # point on an edge, and measured here they do: PostGIS called a boundary point *contained* where
    # shapely called it *touching*. Neither is wrong; the question has no stable answer at that
    # precision.
    #
    # This is precisely why membership uses a hysteresis margin. The tracker demands metres of
    # penetration before asserting anything, so a knife-edge point is never acted upon and the
    # ambiguity cannot reach an event. What must hold — and what is asserted — is that the two agree
    # wherever the answer is not knife-edge.
    knife_edge_m = 0.5
    disagreements = []
    decided = 0
    for point in samples:
        nearest_boundary_m = min(
            (abs(zone.distance_to_boundary_m(point)) for zone in zones), default=999.0
        )
        if nearest_boundary_m < knife_edge_m:
            continue
        decided += 1
        in_memory = sorted(zone.zone_id for zone in index.zones_containing(point))
        in_postgis = sorted(row["zone_id"] for row in await queries.zones_at(point))
        if in_memory != in_postgis:
            disagreements.append((point.lat, point.lon, in_memory, in_postgis))

    assert decided >= 3, f"only {decided} unambiguous sample points; this test would prove nothing"
    assert not disagreements, (
        f"shapely and PostGIS disagree at {len(disagreements)} points more than {knife_edge_m} m "
        f"from any boundary: {disagreements[:3]}"
    )


async def test_every_worldmodel_write_runs_against_postgres(pool, cfg) -> None:  # type: ignore[no-untyped-def]
    """Execute the world model's persistence statements for real, including the empty cases.

    Regression, and an expensive one: the track insert bound a bare placeholder inside a
    ``CASE WHEN ... IS NULL`` test, which Postgres cannot assign a type. EVERY track failed with
    "could not determine data type of parameter $11" — for an entire phase. Nothing looked wrong,
    because the dead-letter queue did its job: each message was rejected, dead-lettered and acked, and
    the pipeline carried on. 23,000 messages in dlq.tracks and every service reporting "ok".

    Two lessons, both now encoded: a write path that is never executed by a test is a write path that
    does not work, and the empty and NULL cases are the ones that break — a track with no path, one
    fix, or three.
    """
    from sio_worldmodel.service import WorldModelService

    from sio_schemas import BBox, Track, TrackState, TrackStatus

    service = WorldModelService.__new__(WorldModelService)
    service.pool = pool
    from sio_core import get_logger

    service.log = get_logger("test.worldmodel")

    cases = {
        "no fixes at all": [],
        "one bbox fix, no geo": [TrackState(ts=utc_now(), bbox=BBox(x1=0, y1=0, x2=5, y2=5))],
        "two geo fixes": [
            TrackState(ts=utc_now(), geo=Geo(lat=37.7749, lon=-122.4194)),
            TrackState(ts=utc_now(), geo=Geo(lat=37.7750, lon=-122.4194)),
        ],
    }
    written = []
    for label, states in cases.items():
        track = Track(
            tenant_id=TENANT,
            source_id="cam-test",
            **{"class": "truck"},
            confidence=0.8,
            status=TrackStatus.CONFIRMED,
            hits=4,
            states=states,
        )
        await service._handle_track(track)
        # Republished on every frame, so the upsert must work too.
        await service._handle_track(track)
        row = await pool.fetchrow(
            "SELECT track_id, path IS NULL AS no_path FROM tracks WHERE tenant_id = %s AND track_id = %s",
            (TENANT, track.track_id),
        )
        assert row is not None, f"track with {label} was not persisted"
        written.append(track.track_id)

    # A path is stored only when there are enough fixes for a linestring.
    paths = await pool.fetch(
        "SELECT track_id, ST_NPoints(path::geometry) AS points FROM tracks "
        "WHERE tenant_id = %s AND track_id = ANY(%s) ORDER BY track_id",
        (TENANT, written),
    )
    assert any(row["points"] == 2 for row in paths), "the two-fix track should have a linestring"
    assert any(row["points"] is None for row in paths), "the no-fix track should have no path"

    await pool.execute(
        "DELETE FROM tracks WHERE tenant_id = %s AND track_id = ANY(%s)", (TENANT, written)
    )


async def test_sensor_readings_are_persisted_as_measurements(pool, cfg) -> None:  # type: ignore[no-untyped-def]
    """The measurements table had no writer at all.

    It existed from the first migration and nothing wrote it, which only became visible once the
    prediction service needed an hour of history to forecast from. A table with no writer looks exactly
    like a table with no data, and neither shows up until something asks — so this executes the write
    path, including the readings that must be *skipped*.
    """
    from sio_worldmodel.service import WorldModelService

    from sio_schemas import Modality, Observation

    service = WorldModelService.__new__(WorldModelService)
    service.pool = pool
    service.settings = cfg
    service._measurement_at = {}
    service._measurements_written = 0
    service._measurements_skipped = 0

    base = utc_now()

    def reading(
        metric: str | None, value: object, *, offset_s: float = 0.0, **extra: object
    ) -> Observation:
        payload: dict[str, object] = dict(extra)
        if metric is not None:
            payload["metric"] = metric
            payload["value"] = value
        return Observation(
            tenant_id=TENANT,
            source_id="iot-test-1",
            modality=Modality.IOT,
            ts=base + timedelta(seconds=offset_s),
            payload=payload,
        )

    await service._record_measurement(
        reading("temperature_c", 21.5, unit="celsius", zone_id="warehouse")
    )
    # A GPS fix carrying its own battery level is a legitimate scalar series.
    await service._record_measurement(
        Observation(
            tenant_id=TENANT,
            source_id="gps-drone-1",
            modality=Modality.GPS,
            ts=base,
            payload={"battery_pct": 62.5, "entity_type": "drone"},
        )
    )
    # Things that must NOT become measurements.
    await service._record_measurement(reading(None, None, tag_id="TAG-1"))
    await service._record_measurement(reading("door_state", "open"))
    # Rate-gated: a second temperature reading one second later.
    await service._record_measurement(reading("temperature_c", 21.6, offset_s=1.0))

    rows = await pool.fetch(
        "SELECT source_id, metric, value, unit, zone_id FROM measurements WHERE tenant_id = %s ORDER BY metric",
        (TENANT,),
    )
    persisted = {(row["source_id"], row["metric"]): row for row in rows}
    assert ("iot-test-1", "temperature_c") in persisted
    assert ("gps-drone-1", "battery_pct") in persisted
    assert persisted[("iot-test-1", "temperature_c")]["value"] == pytest.approx(21.5)
    assert persisted[("iot-test-1", "temperature_c")]["zone_id"] == "warehouse"
    assert not any(row["metric"] == "door_state" for row in rows), (
        "a categorical value is not a series"
    )
    assert len(rows) == 2, f"the RFID read and the rate-gated duplicate must be skipped, got {rows}"
    assert service._measurements_skipped == 1

    await pool.execute("DELETE FROM measurements WHERE tenant_id = %s", (TENANT,))


async def test_every_prediction_query_runs_against_postgres(pool, cfg) -> None:  # type: ignore[no-untyped-def]
    """Execute the prediction service's reads for real.

    Third occurrence of the same class of bug this phase: this one selected ``entity_id`` from ``events``,
    where the column is an ARRAY called ``entities``, and it failed every forecasting cycle with a 500.
    The spatial and world-model paths already had tests like this; prediction did not, so the pattern is
    now applied to every service that talks to the database.

    Runs the whole cycle rather than the individual statements, because the bug was in a query only the
    cycle reaches.
    """
    from sio_core.bus.memory import MemoryBus
    from sio_prediction.service import PredictionService

    # Constructed through __init__ with an in-memory bus, not via __new__ with hand-set attributes.
    # Last phase, a test that built its subject with __new__ masked an attribute __init__ never
    # initialised, and the live service raised on every message while the test stayed green. A test that
    # constructs its subject differently from production is testing a different object.
    service = PredictionService(settings=cfg, bus=MemoryBus())
    service.pool = pool  # the real database; everything else is as production builds it
    await service._load_zones()  # noqa: SLF001

    made_at = utc_now()
    # Each of these hits a different table: events (throughput), events (occupancy), measurements.
    assert isinstance(await service._site_forecasts(made_at), list)  # noqa: SLF001
    assert isinstance(await service._zone_forecasts(made_at), list)  # noqa: SLF001
    assert isinstance(await service._sensor_forecasts(made_at), list)  # noqa: SLF001

    # And the full cycle, which is what the timer calls and what the 500 came from.
    await service.tick()
