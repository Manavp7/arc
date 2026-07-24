"""In-process graph adapter.

Implements the full bitemporal contract (including :meth:`snapshot_at`), so world-model logic
and timeline replay can be unit-tested with no Neo4j and no Postgres.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from sio_schemas import Entity, Relationship

from ..errors import StoreError


class MemoryGraphStore:
    """Dict-backed entity/relationship graph, scoped by tenant."""

    def __init__(self) -> None:
        self._entities: dict[tuple[str, str], Entity] = {}
        self._relationships: dict[tuple[str, str], Relationship] = {}

    # ----------------------------------------------------------------- entities
    async def upsert_entity(self, entity: Entity) -> None:
        key = (entity.tenant_id, entity.entity_id)
        existing = self._entities.get(key)
        if existing is not None:
            # Preserve the earliest sighting and accumulate provenance: an upsert is a merge,
            # not a replace, or every new observation would erase an entity's history.
            entity = entity.model_copy(
                update={
                    "first_seen": min(existing.first_seen, entity.first_seen),
                    "last_seen": max(existing.last_seen, entity.last_seen),
                    "provenance": [*existing.provenance, *entity.provenance][-50:],
                    "track_ids": list(dict.fromkeys([*existing.track_ids, *entity.track_ids])),
                    "attributes": {**existing.attributes, **entity.attributes},
                }
            )
        self._entities[key] = entity

    async def upsert_entities(self, entities: Iterable[Entity]) -> int:
        count = 0
        for entity in entities:
            await self.upsert_entity(entity)
            count += 1
        return count

    async def get_entity(self, entity_id: str, *, tenant_id: str) -> Entity | None:
        return self._entities.get((tenant_id, entity_id))

    async def find_entities(
        self,
        *,
        tenant_id: str,
        entity_type: str | None = None,
        label_contains: str | None = None,
        zone_id: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Entity]:
        found = [
            e
            for (tid, _), e in self._entities.items()
            if tid == tenant_id
            and (entity_type is None or e.type == entity_type)
            and (
                label_contains is None or (e.label or "").lower().find(label_contains.lower()) >= 0
            )
            and (zone_id is None or e.state.zone_id == zone_id)
            and (since is None or e.last_seen >= since)
        ]
        found.sort(key=lambda e: e.last_seen, reverse=True)
        return found[offset : offset + limit]

    # ------------------------------------------------------------ relationships
    async def upsert_relationship(self, relationship: Relationship) -> None:
        self._relationships[(relationship.tenant_id, relationship.id)] = relationship

    async def close_relationship(
        self, relationship_id: str, *, tenant_id: str, ts: datetime
    ) -> None:
        key = (tenant_id, relationship_id)
        existing = self._relationships.get(key)
        if existing is None:
            raise StoreError(f"relationship {relationship_id} not found")
        if existing.ts_valid_to is None:
            self._relationships[key] = existing.model_copy(update={"ts_valid_to": ts})

    async def neighbors(
        self,
        entity_id: str,
        *,
        tenant_id: str,
        types: Sequence[str] | None = None,
        direction: str = "both",
        at: datetime | None = None,
        limit: int = 100,
    ) -> list[tuple[Relationship, Entity]]:
        wanted = {str(t) for t in types} if types else None
        out: list[tuple[Relationship, Entity]] = []
        for (tid, _), rel in self._relationships.items():
            if tid != tenant_id:
                continue
            if wanted and rel.type not in wanted:
                continue
            if at is not None and not rel.holds_at(at):
                continue
            if direction in ("out", "both") and rel.from_id == entity_id:
                other = self._entities.get((tenant_id, rel.to_id))
                if other:
                    out.append((rel, other))
            elif direction in ("in", "both") and rel.to_id == entity_id:
                other = self._entities.get((tenant_id, rel.from_id))
                if other:
                    out.append((rel, other))
            if len(out) >= limit:
                break
        return out

    async def path_between(
        self,
        from_id: str,
        to_id: str,
        *,
        tenant_id: str,
        max_hops: int = 4,
        at: datetime | None = None,
    ) -> list[Relationship]:
        """Breadth-first shortest path, honouring edge validity at ``at``."""
        queue: list[tuple[str, list[Relationship]]] = [(from_id, [])]
        seen = {from_id}
        while queue:
            current, path = queue.pop(0)
            if len(path) >= max_hops:
                continue
            for rel, other in await self.neighbors(
                current, tenant_id=tenant_id, at=at, limit=10_000
            ):
                if other.entity_id in seen:
                    continue
                extended = [*path, rel]
                if other.entity_id == to_id:
                    return extended
                seen.add(other.entity_id)
                queue.append((other.entity_id, extended))
        return []

    async def snapshot_at(
        self, ts: datetime, *, tenant_id: str, limit: int = 1000
    ) -> tuple[list[Entity], list[Relationship]]:
        relationships = [
            rel
            for (tid, _), rel in self._relationships.items()
            if tid == tenant_id and rel.holds_at(ts)
        ][:limit]
        entities = [
            e for (tid, _), e in self._entities.items() if tid == tenant_id and e.first_seen <= ts
        ][:limit]
        return entities, relationships

    async def raw_query(
        self, query: str, params: Mapping[str, Any] | None = None, *, tenant_id: str
    ) -> list[dict[str, Any]]:
        """Not supported: there is no query language here.

        Raising (rather than silently returning nothing) means a copilot tool that needs raw
        traversal fails loudly in a memory-backed test instead of appearing to work.
        """
        raise StoreError(
            "MemoryGraphStore has no query language; use the typed methods, "
            "or select SIO_GRAPH_BACKEND=neo4j|postgres for raw_query"
        )

    async def counts(self, *, tenant_id: str) -> dict[str, int]:
        entities = sum(1 for (tid, _) in self._entities if tid == tenant_id)
        rels = sum(1 for (tid, _) in self._relationships if tid == tenant_id)
        open_rels = sum(
            1 for (tid, _), r in self._relationships.items() if tid == tenant_id and r.is_open
        )
        return {"entities": entities, "relationships": rels, "open_relationships": open_rels}

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    def clear(self) -> None:
        self._entities.clear()
        self._relationships.clear()
