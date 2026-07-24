"""Postgres-backed graph adapter.

Exists so that the world model works — and is testable — without a JVM. It answers the PRD's
open question Q1 ("Neo4j from Phase 0, or Postgres-relational graph first?") by refusing the
premise: the graph lives behind a port, Neo4j is the default runtime, and this adapter is the
fallback for constrained environments, CI, and anyone who does not want to run Neo4j.

Traversal is recursive SQL rather than Cypher. That is slower for deep paths, which is exactly
why Neo4j is the default — but for the site-scale neighbourhoods SIO actually queries
(1-4 hops), it is indistinguishable in practice.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from sio_schemas import Entity, Relationship

from ..errors import StoreError
from ..telemetry import get_logger
from .pg import PgPool

log = get_logger("sio.graph.pg")

_FORBIDDEN_SQL = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "truncate",
    "grant",
    "revoke",
    "copy",
    "create",
)


class PostgresGraphStore:
    """Relational implementation of :class:`~sio_core.ports.GraphStore`."""

    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------ entities
    async def upsert_entity(self, entity: Entity) -> None:
        await self.upsert_entities([entity])

    async def upsert_entities(self, entities: Iterable[Entity]) -> int:
        rows: list[Sequence[Any]] = []
        for entity in entities:
            geo = entity.state.geo
            rows.append(
                (
                    entity.entity_id,
                    entity.tenant_id,
                    str(entity.type),
                    entity.label,
                    entity.confidence,
                    entity.is_static,
                    geo.lon if geo else None,
                    geo.lat if geo else None,
                    entity.state.zone_id,
                    entity.state.h3_cell,
                    entity.first_seen,
                    entity.last_seen,
                    entity.to_json(),
                )
            )
        if not rows:
            return 0
        await self._pool.execute_many(
            """
            INSERT INTO entities (
                entity_id, tenant_id, type, label, confidence, is_static,
                geom, zone_id, h3_cell, first_seen, last_seen, payload
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                -- ST_MakePoint is STRICT, so a NULL coordinate yields a NULL geometry: an
                -- entity without a position (a company, an unlocated sensor) needs no special
                -- case. The explicit casts are required because Postgres cannot infer the type
                -- of a parameter that may be NULL.
                ST_SetSRID(
                    ST_MakePoint(%s::double precision, %s::double precision), 4326
                )::geography,
                %s, %s, %s, %s, %s::jsonb
            )
            ON CONFLICT (tenant_id, entity_id) DO UPDATE SET
                type       = EXCLUDED.type,
                label      = COALESCE(EXCLUDED.label, entities.label),
                confidence = EXCLUDED.confidence,
                is_static  = EXCLUDED.is_static,
                geom       = COALESCE(EXCLUDED.geom, entities.geom),
                zone_id    = COALESCE(EXCLUDED.zone_id, entities.zone_id),
                h3_cell    = COALESCE(EXCLUDED.h3_cell, entities.h3_cell),
                -- An upsert is a merge: LEAST/GREATEST mean a replayed or out-of-order message
                -- can never shrink an entity's known lifetime.
                first_seen = LEAST(entities.first_seen, EXCLUDED.first_seen),
                last_seen  = GREATEST(entities.last_seen, EXCLUDED.last_seen),
                payload    = EXCLUDED.payload || jsonb_build_object(
                                 'first_seen',
                                 to_jsonb(LEAST(entities.first_seen, EXCLUDED.first_seen)),
                                 'last_seen',
                                 to_jsonb(GREATEST(entities.last_seen, EXCLUDED.last_seen))
                             ),
                updated_at = now()
            """,
            rows,
        )
        return len(rows)

    async def get_entity(self, entity_id: str, *, tenant_id: str) -> Entity | None:
        row = await self._pool.fetchrow(
            "SELECT payload FROM entities WHERE tenant_id = %s AND entity_id = %s",
            (tenant_id, entity_id),
        )
        return Entity.model_validate(row["payload"]) if row else None

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
        clauses = ["tenant_id = %s"]
        params: list[Any] = [tenant_id]
        if entity_type:
            clauses.append("type = %s")
            params.append(str(entity_type))
        if label_contains:
            clauses.append("label ILIKE %s")
            params.append(f"%{label_contains}%")
        if zone_id:
            clauses.append("zone_id = %s")
            params.append(zone_id)
        if since:
            clauses.append("last_seen >= %s")
            params.append(since)
        params.extend([limit, offset])
        rows = await self._pool.fetch(
            f"SELECT payload FROM entities WHERE {' AND '.join(clauses)} "
            "ORDER BY last_seen DESC LIMIT %s OFFSET %s",
            params,
        )
        return [Entity.model_validate(r["payload"]) for r in rows]

    # ------------------------------------------------------------ relationships
    async def upsert_relationship(self, relationship: Relationship) -> None:
        await self._pool.execute(
            """
            INSERT INTO relationships (
                rel_id, tenant_id, from_id, type, to_id,
                ts_valid_from, ts_valid_to, confidence, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (tenant_id, rel_id) DO UPDATE SET
                ts_valid_to = EXCLUDED.ts_valid_to,
                confidence  = EXCLUDED.confidence,
                payload     = EXCLUDED.payload
            """,
            (
                relationship.id,
                relationship.tenant_id,
                relationship.from_id,
                str(relationship.type),
                relationship.to_id,
                relationship.ts_valid_from,
                relationship.ts_valid_to,
                relationship.confidence,
                relationship.to_json(),
            ),
        )

    async def close_relationship(
        self, relationship_id: str, *, tenant_id: str, ts: datetime
    ) -> None:
        row = await self._pool.fetchrow(
            "SELECT payload FROM relationships "
            "WHERE tenant_id = %s AND rel_id = %s AND ts_valid_to IS NULL",
            (tenant_id, relationship_id),
        )
        if row is None:
            return
        relationship = Relationship.model_validate(row["payload"])
        relationship.ts_valid_to = ts
        await self.upsert_relationship(relationship)

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
        # Positional parameters must be assembled in the order they appear in the SQL text, and
        # the JOIN precedes the WHERE. Building the list in textual order is what keeps this
        # correct: an earlier version collected the WHERE parameters first and therefore bound
        # tenant_id into the JOIN's CASE, silently returning the wrong neighbours.
        join_params: list[Any] = [entity_id, tenant_id]

        clauses = ["r.tenant_id = %s"]
        where_params: list[Any] = [tenant_id]
        if direction == "out":
            clauses.append("r.from_id = %s")
            where_params.append(entity_id)
        elif direction == "in":
            clauses.append("r.to_id = %s")
            where_params.append(entity_id)
        else:
            clauses.append("(r.from_id = %s OR r.to_id = %s)")
            where_params.extend([entity_id, entity_id])
        if types:
            clauses.append("r.type = ANY(%s)")
            where_params.append([str(t) for t in types])
        if at is not None:
            clauses.append(
                "r.ts_valid_from <= %s AND (r.ts_valid_to IS NULL OR r.ts_valid_to >= %s)"
            )
            where_params.extend([at, at])

        rows = await self._pool.fetch(
            f"""
            SELECT r.payload AS rel, e.payload AS entity
              FROM relationships r
              JOIN entities e
                ON e.entity_id = CASE WHEN r.from_id = %s THEN r.to_id ELSE r.from_id END
               AND e.tenant_id = %s
             WHERE {" AND ".join(clauses)}
             LIMIT %s
            """,
            [*join_params, *where_params, limit],
        )
        return [
            (Relationship.model_validate(r["rel"]), Entity.model_validate(r["entity"]))
            for r in rows
        ]

    async def path_between(
        self,
        from_id: str,
        to_id: str,
        *,
        tenant_id: str,
        max_hops: int = 4,
        at: datetime | None = None,
    ) -> list[Relationship]:
        rows = await self._pool.fetch(
            """
            WITH RECURSIVE walk(node, path, depth) AS (
                SELECT %s::text, ARRAY[]::text[], 0
              UNION ALL
                SELECT CASE WHEN r.from_id = w.node THEN r.to_id ELSE r.from_id END,
                       w.path || r.rel_id,
                       w.depth + 1
                  FROM walk w
                  JOIN relationships r
                    ON (r.from_id = w.node OR r.to_id = w.node)
                   AND r.tenant_id = %s
                   AND (%s::timestamptz IS NULL
                        OR (r.ts_valid_from <= %s
                            AND (r.ts_valid_to IS NULL OR r.ts_valid_to >= %s)))
                 WHERE w.depth < %s
                   AND NOT (r.rel_id = ANY(w.path))
            )
            SELECT path FROM walk WHERE node = %s ORDER BY depth LIMIT 1
            """,
            (from_id, tenant_id, at, at, at, max_hops, to_id),
        )
        if not rows or not rows[0]["path"]:
            return []
        ids = list(rows[0]["path"])
        payloads = await self._pool.fetch(
            "SELECT rel_id, payload FROM relationships WHERE tenant_id = %s AND rel_id = ANY(%s)",
            (tenant_id, ids),
        )
        by_id = {r["rel_id"]: Relationship.model_validate(r["payload"]) for r in payloads}
        return [by_id[i] for i in ids if i in by_id]

    async def snapshot_at(
        self, ts: datetime, *, tenant_id: str, limit: int = 1000
    ) -> tuple[list[Entity], list[Relationship]]:
        entity_rows = await self._pool.fetch(
            "SELECT payload FROM entities WHERE tenant_id = %s AND first_seen <= %s "
            "ORDER BY last_seen DESC LIMIT %s",
            (tenant_id, ts, limit),
        )
        rel_rows = await self._pool.fetch(
            "SELECT payload FROM relationships WHERE tenant_id = %s AND ts_valid_from <= %s "
            "AND (ts_valid_to IS NULL OR ts_valid_to >= %s) LIMIT %s",
            (tenant_id, ts, ts, limit),
        )
        return (
            [Entity.model_validate(r["payload"]) for r in entity_rows],
            [Relationship.model_validate(r["payload"]) for r in rel_rows],
        )

    async def raw_query(
        self, query: str, params: Mapping[str, Any] | None = None, *, tenant_id: str
    ) -> list[dict[str, Any]]:
        """Run a read-only SQL query. Writes and multi-statement input are refused."""
        lowered = query.lower()
        if any(word in lowered for word in _FORBIDDEN_SQL):
            raise StoreError("raw_query is read-only; DDL/DML keywords are not permitted")
        if ";" in query.strip().rstrip(";"):
            raise StoreError("raw_query accepts a single statement")
        # Pass the mapping straight through: psycopg binds %(name)s from a dict, and flattening
        # to a list would break named placeholders (which is the natural style for a
        # copilot-authored query, and how this first broke).
        merged = {"tenant_id": tenant_id, **dict(params or {})} if params is not None else None
        rows = await self._pool.fetch(query, merged)
        return [json.loads(json.dumps(row, default=str)) for row in rows]

    async def counts(self, *, tenant_id: str) -> dict[str, int]:
        row = await self._pool.fetchrow(
            """
            SELECT (SELECT count(*) FROM entities WHERE tenant_id = %s) AS entities,
                   (SELECT count(*) FROM relationships WHERE tenant_id = %s) AS relationships,
                   (SELECT count(*) FROM relationships
                     WHERE tenant_id = %s AND ts_valid_to IS NULL) AS open_relationships
            """,
            (tenant_id, tenant_id, tenant_id),
        )
        return {k: int(v or 0) for k, v in (row or {}).items()}

    async def ping(self) -> bool:
        return await self._pool.ping()

    async def close(self) -> None:
        await self._pool.close()
