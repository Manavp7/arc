"""Neo4j graph adapter — the default world-model backend (PRD M7).

Design notes worth knowing before editing:

- **Lossless payload + indexed projections.** The whole entity is stored as a JSON string in
  ``payload`` so reads round-trip exactly, *plus* a handful of scalar properties (type, label,
  position, timestamps as epoch millis) that exist purely so Cypher can filter and index.
  Storing only decomposed properties would lose nested attributes; storing only JSON would
  make every query a full scan.
- **Epoch millis for time.** Bitemporal comparisons (`valid_from_ms`, `valid_to_ms`) are
  integer comparisons, which index cleanly and behave identically across driver versions.
- **Relationship types are interpolated from a closed enum.** Cypher cannot parameterise a
  relationship type; interpolating an arbitrary string would be an injection hole, so the
  value is validated against :class:`~sio_schemas.RelationshipType` first.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sio_schemas import Entity, Relationship, RelationshipType

from ..errors import DependencyMissing, StoreError
from ..telemetry import get_logger

log = get_logger("sio.graph.neo4j")

_WRITE_KEYWORDS = (
    "create",
    "merge",
    "delete",
    "detach",
    "set ",
    "remove",
    "drop",
    "load csv",
    "call db.",
    "foreach",
)


def _ms(ts: datetime | None) -> int | None:
    return int(ts.timestamp() * 1000) if ts else None


def _validate_rel_type(value: str) -> str:
    try:
        return RelationshipType(value).value.upper()
    except ValueError as exc:
        raise StoreError(f"unknown relationship type {value!r}") from exc


class Neo4jGraphStore:
    """Async Neo4j-backed :class:`~sio_core.ports.GraphStore`."""

    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j") -> None:
        try:
            from neo4j import AsyncGraphDatabase
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise DependencyMissing("neo4j", "Neo4jGraphStore") from exc
        self._driver: Any = AsyncGraphDatabase.driver(uri, auth=(user, password))
        self._database = database

    # ----------------------------------------------------------------- internals
    async def _run(
        self, query: str, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        try:
            async with self._driver.session(database=self._database) as session:
                result = await session.run(query, dict(params or {}))
                return [dict(record) async for record in result]
        except Exception as exc:
            raise StoreError(f"neo4j query failed: {exc}") from exc

    @staticmethod
    def _entity_props(entity: Entity) -> dict[str, Any]:
        geo = entity.state.geo
        return {
            "entity_id": entity.entity_id,
            "tenant_id": entity.tenant_id,
            "type": str(entity.type),
            "label": entity.label,
            "confidence": entity.confidence,
            "is_static": entity.is_static,
            "lat": geo.lat if geo else None,
            "lon": geo.lon if geo else None,
            "alt": geo.alt if geo else None,
            "zone_id": entity.state.zone_id,
            "h3_cell": entity.state.h3_cell,
            "first_seen_ms": _ms(entity.first_seen),
            "last_seen_ms": _ms(entity.last_seen),
            "first_seen": entity.first_seen.isoformat(),
            "last_seen": entity.last_seen.isoformat(),
            "payload": entity.to_json(),
        }

    @staticmethod
    def _to_entity(payload: str | None) -> Entity | None:
        if not payload:
            return None
        return Entity.model_validate_json(payload)

    # ------------------------------------------------------------------ entities
    async def upsert_entity(self, entity: Entity) -> None:
        await self.upsert_entities([entity])

    async def upsert_entities(self, entities: Iterable[Entity]) -> int:
        rows = [self._entity_props(e) for e in entities]
        if not rows:
            return 0
        # first_seen is kept at its minimum and last_seen at its maximum so that replaying or
        # reordering messages can never shrink an entity's known lifetime.
        await self._run(
            """
            UNWIND $rows AS row
            MERGE (e:Entity {entity_id: row.entity_id, tenant_id: row.tenant_id})
            ON CREATE SET e += row
            ON MATCH SET
                e.type          = row.type,
                e.label         = coalesce(row.label, e.label),
                e.confidence    = row.confidence,
                e.is_static     = row.is_static,
                e.lat           = coalesce(row.lat, e.lat),
                e.lon           = coalesce(row.lon, e.lon),
                e.alt           = coalesce(row.alt, e.alt),
                e.zone_id       = coalesce(row.zone_id, e.zone_id),
                e.h3_cell       = coalesce(row.h3_cell, e.h3_cell),
                e.first_seen_ms = CASE WHEN row.first_seen_ms < e.first_seen_ms
                                       THEN row.first_seen_ms ELSE e.first_seen_ms END,
                e.last_seen_ms  = CASE WHEN row.last_seen_ms > e.last_seen_ms
                                       THEN row.last_seen_ms ELSE e.last_seen_ms END,
                e.first_seen    = CASE WHEN row.first_seen_ms < e.first_seen_ms
                                       THEN row.first_seen ELSE e.first_seen END,
                e.last_seen     = CASE WHEN row.last_seen_ms > e.last_seen_ms
                                       THEN row.last_seen ELSE e.last_seen END,
                e.payload       = row.payload
            """,
            {"rows": rows},
        )
        return len(rows)

    async def get_entity(self, entity_id: str, *, tenant_id: str) -> Entity | None:
        rows = await self._run(
            "MATCH (e:Entity {entity_id: $entity_id, tenant_id: $tenant_id}) "
            "RETURN e.payload AS payload LIMIT 1",
            {"entity_id": entity_id, "tenant_id": tenant_id},
        )
        return self._to_entity(rows[0]["payload"]) if rows else None

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
        clauses = ["e.tenant_id = $tenant_id"]
        params: dict[str, Any] = {"tenant_id": tenant_id, "limit": limit, "offset": offset}
        if entity_type:
            clauses.append("e.type = $type")
            params["type"] = str(entity_type)
        if label_contains:
            clauses.append("toLower(coalesce(e.label, '')) CONTAINS toLower($label)")
            params["label"] = label_contains
        if zone_id:
            clauses.append("e.zone_id = $zone_id")
            params["zone_id"] = zone_id
        if since:
            clauses.append("e.last_seen_ms >= $since_ms")
            params["since_ms"] = _ms(since)

        rows = await self._run(
            f"MATCH (e:Entity) WHERE {' AND '.join(clauses)} "
            "RETURN e.payload AS payload ORDER BY e.last_seen_ms DESC SKIP $offset LIMIT $limit",
            params,
        )
        return [e for e in (self._to_entity(r["payload"]) for r in rows) if e is not None]

    # ------------------------------------------------------------ relationships
    async def upsert_relationship(self, relationship: Relationship) -> None:
        rel_type = _validate_rel_type(str(relationship.type))
        await self._run(
            f"""
            MERGE (a:Entity {{entity_id: $from_id, tenant_id: $tenant_id}})
            MERGE (b:Entity {{entity_id: $to_id,   tenant_id: $tenant_id}})
            MERGE (a)-[r:{rel_type} {{rel_id: $rel_id}}]->(b)
            SET r.tenant_id     = $tenant_id,
                r.type          = $type,
                r.valid_from_ms = $valid_from_ms,
                r.valid_to_ms   = $valid_to_ms,
                r.confidence    = $confidence,
                r.payload       = $payload
            """,
            {
                "rel_id": relationship.id,
                "tenant_id": relationship.tenant_id,
                "from_id": relationship.from_id,
                "to_id": relationship.to_id,
                "type": str(relationship.type),
                "valid_from_ms": _ms(relationship.ts_valid_from),
                "valid_to_ms": _ms(relationship.ts_valid_to),
                "confidence": relationship.confidence,
                "payload": relationship.to_json(),
            },
        )

    async def close_relationship(
        self, relationship_id: str, *, tenant_id: str, ts: datetime
    ) -> None:
        rows = await self._run(
            """
            MATCH ()-[r {rel_id: $rel_id, tenant_id: $tenant_id}]->()
            WHERE r.valid_to_ms IS NULL
            RETURN r.payload AS payload LIMIT 1
            """,
            {"rel_id": relationship_id, "tenant_id": tenant_id},
        )
        if not rows:
            return
        relationship = Relationship.model_validate_json(rows[0]["payload"])
        relationship.ts_valid_to = ts
        await self._run(
            """
            MATCH ()-[r {rel_id: $rel_id, tenant_id: $tenant_id}]->()
            SET r.valid_to_ms = $valid_to_ms, r.payload = $payload
            """,
            {
                "rel_id": relationship_id,
                "tenant_id": tenant_id,
                "valid_to_ms": _ms(ts),
                "payload": relationship.to_json(),
            },
        )

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
        pattern = {
            "out": "(a:Entity)-[r]->(b:Entity)",
            "in": "(a:Entity)<-[r]-(b:Entity)",
        }.get(direction, "(a:Entity)-[r]-(b:Entity)")
        clauses = [
            "a.entity_id = $entity_id",
            "a.tenant_id = $tenant_id",
            "b.tenant_id = $tenant_id",
        ]
        params: dict[str, Any] = {"entity_id": entity_id, "tenant_id": tenant_id, "limit": limit}
        if types:
            clauses.append("r.type IN $types")
            params["types"] = [str(t) for t in types]
        if at is not None:
            clauses.append(
                "r.valid_from_ms <= $at_ms AND (r.valid_to_ms IS NULL OR r.valid_to_ms >= $at_ms)"
            )
            params["at_ms"] = _ms(at)

        rows = await self._run(
            f"MATCH {pattern} WHERE {' AND '.join(clauses)} "
            "RETURN r.payload AS rel, b.payload AS entity LIMIT $limit",
            params,
        )
        out: list[tuple[Relationship, Entity]] = []
        for row in rows:
            entity = self._to_entity(row["entity"])
            if row["rel"] and entity is not None:
                out.append((Relationship.model_validate_json(row["rel"]), entity))
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
        at_ms = _ms(at)
        rows = await self._run(
            f"""
            MATCH path = shortestPath(
                (a:Entity {{entity_id: $from_id, tenant_id: $tenant_id}})
                -[*..{int(max_hops)}]-
                (b:Entity {{entity_id: $to_id, tenant_id: $tenant_id}})
            )
            WHERE all(r IN relationships(path) WHERE
                      $at_ms IS NULL OR (r.valid_from_ms <= $at_ms AND
                      (r.valid_to_ms IS NULL OR r.valid_to_ms >= $at_ms)))
            RETURN [r IN relationships(path) | r.payload] AS payloads LIMIT 1
            """,
            {"from_id": from_id, "to_id": to_id, "tenant_id": tenant_id, "at_ms": at_ms},
        )
        if not rows or not rows[0]["payloads"]:
            return []
        return [Relationship.model_validate_json(p) for p in rows[0]["payloads"] if p]

    async def snapshot_at(
        self, ts: datetime, *, tenant_id: str, limit: int = 1000
    ) -> tuple[list[Entity], list[Relationship]]:
        at_ms = _ms(ts)
        entity_rows = await self._run(
            "MATCH (e:Entity) WHERE e.tenant_id = $tenant_id AND e.first_seen_ms <= $at_ms "
            "RETURN e.payload AS payload ORDER BY e.last_seen_ms DESC LIMIT $limit",
            {"tenant_id": tenant_id, "at_ms": at_ms, "limit": limit},
        )
        rel_rows = await self._run(
            """
            MATCH ()-[r]->()
            WHERE r.tenant_id = $tenant_id
              AND r.valid_from_ms <= $at_ms
              AND (r.valid_to_ms IS NULL OR r.valid_to_ms >= $at_ms)
            RETURN r.payload AS payload LIMIT $limit
            """,
            {"tenant_id": tenant_id, "at_ms": at_ms, "limit": limit},
        )
        entities = [e for e in (self._to_entity(r["payload"]) for r in entity_rows) if e]
        rels = [Relationship.model_validate_json(r["payload"]) for r in rel_rows if r["payload"]]
        return entities, rels

    async def raw_query(
        self, query: str, params: Mapping[str, Any] | None = None, *, tenant_id: str
    ) -> list[dict[str, Any]]:
        """Run a read-only Cypher query, with ``$tenant_id`` always bound.

        Writes are refused: this path exists for the copilot's ``graph_query`` tool, and an
        LLM-authored mutation is not a risk worth carrying.
        """
        lowered = query.lower()
        if any(keyword in lowered for keyword in _WRITE_KEYWORDS):
            raise StoreError("raw_query is read-only; write keywords are not permitted")
        merged = {"tenant_id": tenant_id, **dict(params or {})}
        rows = await self._run(query, merged)
        return [self._jsonable(row) for row in rows]

    @staticmethod
    def _jsonable(row: Mapping[str, Any]) -> dict[str, Any]:
        """Flatten driver types so results can be serialised into an API response."""
        out: dict[str, Any] = {}
        for key, value in row.items():
            if hasattr(value, "isoformat"):
                out[key] = value.isoformat()
            elif isinstance(value, (dict, list, str, int, float, bool)) or value is None:
                out[key] = value
            elif hasattr(value, "items"):  # neo4j Node / Relationship
                out[key] = {k: v for k, v in value.items() if k != "payload"}
            else:
                out[key] = str(value)
        return out

    async def counts(self, *, tenant_id: str) -> dict[str, int]:
        rows = await self._run(
            """
            MATCH (e:Entity {tenant_id: $tenant_id})
            WITH count(e) AS entities
            OPTIONAL MATCH ()-[r]->() WHERE r.tenant_id = $tenant_id
            RETURN entities,
                   count(r) AS relationships,
                   sum(CASE WHEN r.valid_to_ms IS NULL THEN 1 ELSE 0 END) AS open_relationships
            """,
            {"tenant_id": tenant_id},
        )
        if not rows:
            return {"entities": 0, "relationships": 0, "open_relationships": 0}
        row = rows[0]
        return {
            "entities": int(row.get("entities") or 0),
            "relationships": int(row.get("relationships") or 0),
            "open_relationships": int(row.get("open_relationships") or 0),
        }

    async def apply_constraints(self, statements: Iterable[str]) -> int:
        """Apply schema constraints/indexes (used by ``scripts/init_neo4j.py``)."""
        applied = 0
        for statement in statements:
            text = statement.strip().rstrip(";")
            if not text or text.startswith("//"):
                continue
            await self._run(text)
            applied += 1
        return applied

    async def ping(self) -> bool:
        try:
            rows = await self._run("RETURN 1 AS ok")
            return bool(rows and rows[0].get("ok") == 1)
        except Exception:
            return False

    async def close(self) -> None:
        await self._driver.close()

    @staticmethod
    def dump_json(value: Any) -> str:
        return json.dumps(value, default=str)


def utc(ts: datetime) -> datetime:
    return ts.astimezone(UTC)
