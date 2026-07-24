"""World-model graph contract, exercised against the in-memory adapter.

The same suite is reused against Neo4j and Postgres in ``tests/integration`` once
infrastructure is available — the assertions here define what "a graph store" means in SIO,
above all the bitemporal behaviour that timeline replay (UC5) depends on.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from sio_core.errors import StoreError
from sio_core.stores.graph_memory import MemoryGraphStore
from sio_schemas import (
    Entity,
    EntityState,
    Geo,
    Provenance,
    Relationship,
    RelationshipType,
    utc_now,
)
from sio_schemas.enums import Modality

TENANT = "default"
OTHER_TENANT = "acme"


@pytest.fixture
def graph() -> MemoryGraphStore:
    return MemoryGraphStore()


def truck(entity_id: str = "ent_truck", label: str = "Truck ABC-123") -> Entity:
    return Entity(
        entity_id=entity_id,
        type="truck",
        label=label,
        state=EntityState(geo=Geo(lat=37.7749, lon=-122.4194), zone_id="yard"),
    )


async def test_upsert_and_get(graph: MemoryGraphStore) -> None:
    await graph.upsert_entity(truck())
    found = await graph.get_entity("ent_truck", tenant_id=TENANT)
    assert found is not None and found.label == "Truck ABC-123"


async def test_upsert_protects_the_lifetime_bounds(graph: MemoryGraphStore) -> None:
    """The merge contract: first_seen never moves later, last_seen never moves earlier.

    This is what keeps a replayed or out-of-order message from shrinking what is known about an
    entity — and it is what makes dwell time trustworthy (UC1). Everything *else* in the payload
    belongs to the producer (fusion owns attributes and provenance, PRD M5), which is why this test
    asserts the bounds and deliberately does not assert attribute merging.
    """
    start = utc_now()
    await graph.upsert_entity(
        truck().model_copy(
            update={
                "first_seen": start,
                "last_seen": start,
                "provenance": [
                    Provenance(source_id="cam-gate-a", modality=Modality.VIDEO, ts=start)
                ],
                "attributes": {"plate": "ABC-123"},
            }
        )
    )
    later = start + timedelta(minutes=20)
    await graph.upsert_entity(
        truck().model_copy(
            update={
                "first_seen": later,  # a fresh producer claiming "I just saw this for the first time"
                "last_seen": later,
                "provenance": [Provenance(source_id="gps-1", modality=Modality.GPS, ts=later)],
                "attributes": {"plate": "ABC-123", "colour": "red"},
            }
        )
    )

    merged = await graph.get_entity("ent_truck", tenant_id=TENANT)
    assert merged is not None
    assert merged.first_seen == start, "earliest sighting is preserved"
    assert merged.last_seen == later, "latest sighting wins"
    assert merged.dwell_s() == pytest.approx(1200), "dwell time survives a re-publish"
    assert merged.attributes["colour"] == "red", "the producer's payload is authoritative"


async def test_out_of_order_delivery_cannot_rewind_last_seen(graph: MemoryGraphStore) -> None:
    """At-least-once delivery reorders messages; an old one must not undo a newer one."""
    start = utc_now()
    latest = start + timedelta(minutes=5)
    await graph.upsert_entity(truck().model_copy(update={"first_seen": start, "last_seen": latest}))
    await graph.upsert_entity(
        truck().model_copy(update={"first_seen": start, "last_seen": start})  # stale redelivery
    )
    found = await graph.get_entity("ent_truck", tenant_id=TENANT)
    assert found is not None and found.last_seen == latest


async def test_find_entities_filters(graph: MemoryGraphStore) -> None:
    await graph.upsert_entities(
        [
            truck("ent_a", "Truck A"),
            truck("ent_b", "Truck B"),
            Entity(entity_id="ent_p", type="person", label="Worker"),
        ]
    )
    assert len(await graph.find_entities(tenant_id=TENANT)) == 3
    assert len(await graph.find_entities(tenant_id=TENANT, entity_type="truck")) == 2
    assert len(await graph.find_entities(tenant_id=TENANT, label_contains="worker")) == 1
    assert len(await graph.find_entities(tenant_id=TENANT, zone_id="yard")) == 2
    assert len(await graph.find_entities(tenant_id=TENANT, limit=1)) == 1


async def test_tenant_isolation(graph: MemoryGraphStore) -> None:
    """Cross-tenant leakage is the failure nobody notices until it matters."""
    await graph.upsert_entity(truck())
    await graph.upsert_entity(
        truck("ent_other", "Other tenant truck").model_copy(update={"tenant_id": OTHER_TENANT})
    )
    assert await graph.get_entity("ent_other", tenant_id=TENANT) is None
    assert len(await graph.find_entities(tenant_id=TENANT)) == 1
    assert len(await graph.find_entities(tenant_id=OTHER_TENANT)) == 1


async def test_neighbors_direction_and_type(graph: MemoryGraphStore) -> None:
    await graph.upsert_entities([truck(), Entity(entity_id="cam_1", type="camera", is_static=True)])
    await graph.upsert_relationship(
        Relationship(**{"from": "ent_truck", "to": "cam_1"}, type=RelationshipType.SEEN_BY)
    )

    out = await graph.neighbors("ent_truck", tenant_id=TENANT, direction="out")
    assert [e.entity_id for _, e in out] == ["cam_1"]
    assert await graph.neighbors("ent_truck", tenant_id=TENANT, direction="in") == []
    inbound = await graph.neighbors("cam_1", tenant_id=TENANT, direction="in")
    assert [e.entity_id for _, e in inbound] == ["ent_truck"]
    assert await graph.neighbors("ent_truck", tenant_id=TENANT, types=[RelationshipType.OWNS]) == []


async def test_neighbors_respects_edge_validity_at_a_past_instant(
    graph: MemoryGraphStore,
) -> None:
    """'Which camera last saw entity X?' (UC3) is a time-scoped traversal."""
    t0 = utc_now()
    await graph.upsert_entities([truck(), Entity(entity_id="cam_1", type="camera")])
    await graph.upsert_relationship(
        Relationship(
            **{"from": "ent_truck", "to": "cam_1"},
            type=RelationshipType.SEEN_BY,
            ts_valid_from=t0,
            ts_valid_to=t0 + timedelta(minutes=5),
        )
    )
    assert (
        len(await graph.neighbors("ent_truck", tenant_id=TENANT, at=t0 + timedelta(minutes=1))) == 1
    )
    assert await graph.neighbors("ent_truck", tenant_id=TENANT, at=t0 + timedelta(minutes=9)) == []
    assert await graph.neighbors("ent_truck", tenant_id=TENANT, at=t0 - timedelta(minutes=1)) == []


async def test_close_relationship_keeps_history(graph: MemoryGraphStore) -> None:
    t0 = utc_now()
    rel = Relationship(
        **{"from": "ent_truck", "to": "dock_3"}, type=RelationshipType.CONTAINS, ts_valid_from=t0
    )
    await graph.upsert_entities([truck(), Entity(entity_id="dock_3", type="dock")])
    await graph.upsert_relationship(rel)
    await graph.close_relationship(rel.id, tenant_id=TENANT, ts=t0 + timedelta(minutes=3))

    counts = await graph.counts(tenant_id=TENANT)
    assert counts["relationships"] == 1, "closing must not delete the edge"
    assert counts["open_relationships"] == 0
    still_there = await graph.neighbors("ent_truck", tenant_id=TENANT, at=t0 + timedelta(minutes=1))
    assert len(still_there) == 1, "the past is still queryable"


async def test_close_relationship_is_idempotent(graph: MemoryGraphStore) -> None:
    t0 = utc_now()
    rel = Relationship(**{"from": "a", "to": "b"}, type=RelationshipType.NEAR, ts_valid_from=t0)
    await graph.upsert_relationship(rel)
    await graph.close_relationship(rel.id, tenant_id=TENANT, ts=t0 + timedelta(seconds=10))
    await graph.close_relationship(rel.id, tenant_id=TENANT, ts=t0 + timedelta(seconds=99))
    _, rels = await graph.snapshot_at(t0 + timedelta(seconds=5), tenant_id=TENANT)
    assert rels[0].ts_valid_to == t0 + timedelta(seconds=10), "first close wins"


async def test_close_unknown_relationship_raises(graph: MemoryGraphStore) -> None:
    with pytest.raises(StoreError):
        await graph.close_relationship("rel_missing", tenant_id=TENANT, ts=utc_now())


async def test_path_between(graph: MemoryGraphStore) -> None:
    await graph.upsert_entities(
        [
            truck(),
            Entity(entity_id="cam_1", type="camera"),
            Entity(entity_id="zone_dock", type="zone"),
        ]
    )
    await graph.upsert_relationship(
        Relationship(**{"from": "ent_truck", "to": "cam_1"}, type=RelationshipType.SEEN_BY)
    )
    await graph.upsert_relationship(
        Relationship(**{"from": "cam_1", "to": "zone_dock"}, type=RelationshipType.COVERS)
    )

    path = await graph.path_between("ent_truck", "zone_dock", tenant_id=TENANT)
    assert [r.type for r in path] == [RelationshipType.SEEN_BY, RelationshipType.COVERS]
    assert await graph.path_between("ent_truck", "zone_dock", tenant_id=TENANT, max_hops=1) == []
    assert await graph.path_between("ent_truck", "nowhere", tenant_id=TENANT) == []


async def test_snapshot_at_reconstructs_a_past_world(graph: MemoryGraphStore) -> None:
    """The core of UC5: the graph as it stood, not as it stands."""
    t0 = utc_now()
    await graph.upsert_entity(truck().model_copy(update={"first_seen": t0, "last_seen": t0}))
    later = t0 + timedelta(minutes=10)
    await graph.upsert_entity(
        Entity(entity_id="ent_late", type="person", first_seen=later, last_seen=later)
    )
    await graph.upsert_relationship(
        Relationship(
            **{"from": "ent_truck", "to": "ent_late"},
            type=RelationshipType.NEAR,
            ts_valid_from=later,
        )
    )

    past_entities, past_rels = await graph.snapshot_at(t0 + timedelta(minutes=1), tenant_id=TENANT)
    assert {e.entity_id for e in past_entities} == {"ent_truck"}
    assert past_rels == []

    now_entities, now_rels = await graph.snapshot_at(later + timedelta(minutes=1), tenant_id=TENANT)
    assert {e.entity_id for e in now_entities} == {"ent_truck", "ent_late"}
    assert len(now_rels) == 1


async def test_raw_query_is_refused_loudly(graph: MemoryGraphStore) -> None:
    """Better to fail a memory-backed test than to let a copilot tool look like it works."""
    with pytest.raises(StoreError, match="no query language"):
        await graph.raw_query("MATCH (n) RETURN n", tenant_id=TENANT)


async def test_counts_and_ping(graph: MemoryGraphStore) -> None:
    await graph.upsert_entity(truck())
    assert (await graph.counts(tenant_id=TENANT))["entities"] == 1
    assert await graph.ping() is True
    await graph.close()
