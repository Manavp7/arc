"""Vector store adapters: pgvector (default) and in-memory.

pgvector rather than a separate vector database because it keeps embeddings in the same
transaction and the same tenant-scoped query as the structured data they describe — a
semantic search that also needs "and only trucks, and only in zone dock-3" is one SQL
statement instead of two round trips and a join in application code (PRD §9.2).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from ..errors import StoreError
from ..telemetry import get_logger
from .pg import PgPool

log = get_logger("sio.vectors")

DEFAULT_DIM = 512
"""512 because both CLIP ViT-B/32 and the YOLO26 ReID head emit 512-d vectors, so frame
search and appearance re-identification share one column type."""


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class MemoryVectorStore:
    """Exact-search vector store for tests and infra-free runs."""

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        self.dim = dim
        self._items: dict[tuple[str, str, str], tuple[list[float], dict[str, Any]]] = {}

    async def upsert(
        self,
        collection: str,
        item_id: str,
        vector: Sequence[float],
        *,
        tenant_id: str,
        metadata: Mapping[str, Any] | None = None,
        ts: datetime | None = None,
    ) -> None:
        if len(vector) != self.dim:
            raise StoreError(f"expected {self.dim}-d vector, got {len(vector)}")
        meta = dict(metadata or {})
        if ts is not None:
            meta.setdefault("ts", ts.isoformat())
        self._items[(tenant_id, collection, item_id)] = (list(vector), meta)

    async def search(
        self,
        collection: str,
        vector: Sequence[float],
        *,
        tenant_id: str,
        limit: int = 10,
        filters: Mapping[str, Any] | None = None,
        min_score: float | None = None,
    ) -> list[tuple[str, float, dict[str, Any]]]:
        scored: list[tuple[str, float, dict[str, Any]]] = []
        for (tid, coll, item_id), (vec, meta) in self._items.items():
            if tid != tenant_id or coll != collection:
                continue
            if filters and any(meta.get(k) != v for k, v in filters.items()):
                continue
            score = cosine_similarity(vector, vec)
            if min_score is not None and score < min_score:
                continue
            scored.append((item_id, score, meta))
        scored.sort(key=lambda row: row[1], reverse=True)
        return scored[:limit]

    async def get(
        self, collection: str, item_id: str, *, tenant_id: str
    ) -> tuple[list[float], dict[str, Any]] | None:
        return self._items.get((tenant_id, collection, item_id))

    async def delete(self, collection: str, item_id: str, *, tenant_id: str) -> None:
        self._items.pop((tenant_id, collection, item_id), None)

    async def count(self, collection: str, *, tenant_id: str) -> int:
        return sum(1 for (tid, coll, _) in self._items if tid == tenant_id and coll == collection)

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    def clear(self) -> None:
        self._items.clear()


class PgVectorStore:
    """pgvector-backed store over the ``embeddings`` table (see ``infra/postgres``)."""

    def __init__(self, pool: PgPool, dim: int = DEFAULT_DIM) -> None:
        self._pool = pool
        self.dim = dim

    @staticmethod
    def _literal(vector: Sequence[float]) -> str:
        # pgvector accepts its own text form; formatting it here avoids requiring the
        # `pgvector.psycopg` adapter to be registered on every connection in the pool.
        return "[" + ",".join(f"{float(v):.6g}" for v in vector) + "]"

    async def upsert(
        self,
        collection: str,
        item_id: str,
        vector: Sequence[float],
        *,
        tenant_id: str,
        metadata: Mapping[str, Any] | None = None,
        ts: datetime | None = None,
    ) -> None:
        if len(vector) != self.dim:
            raise StoreError(f"expected {self.dim}-d vector, got {len(vector)}")
        import json

        await self._pool.execute(
            """
            INSERT INTO embeddings (tenant_id, collection, item_id, embedding, metadata, ts)
            VALUES (%s, %s, %s, %s::vector, %s::jsonb, COALESCE(%s, now()))
            ON CONFLICT (tenant_id, collection, item_id) DO UPDATE
               SET embedding = EXCLUDED.embedding,
                   metadata  = EXCLUDED.metadata,
                   ts        = EXCLUDED.ts
            """,
            (
                tenant_id,
                collection,
                item_id,
                self._literal(vector),
                json.dumps(dict(metadata or {})),
                ts,
            ),
        )

    async def search(
        self,
        collection: str,
        vector: Sequence[float],
        *,
        tenant_id: str,
        limit: int = 10,
        filters: Mapping[str, Any] | None = None,
        min_score: float | None = None,
    ) -> list[tuple[str, float, dict[str, Any]]]:
        import json

        clauses = ["tenant_id = %s", "collection = %s"]
        params: list[Any] = [tenant_id, collection]
        if filters:
            clauses.append("metadata @> %s::jsonb")
            params.append(json.dumps(dict(filters)))
        params.append(self._literal(vector))
        params.append(limit)

        rows = await self._pool.fetch(
            f"""
            SELECT item_id,
                   1 - (embedding <=> %s::vector) AS score,
                   metadata
              FROM embeddings
             WHERE {" AND ".join(clauses)}
             ORDER BY embedding <=> %s::vector
             LIMIT %s
            """,
            # The distance expression appears twice (select + order by), so the vector is bound
            # twice; keeping them positional avoids a named-parameter dependency.
            [params[-2], *params[:-2], params[-2], params[-1]],
        )
        out: list[tuple[str, float, dict[str, Any]]] = []
        for row in rows:
            score = float(row["score"])
            if min_score is not None and score < min_score:
                continue
            metadata = row["metadata"] or {}
            out.append((row["item_id"], score, dict(metadata)))
        return out

    async def get(
        self, collection: str, item_id: str, *, tenant_id: str
    ) -> tuple[list[float], dict[str, Any]] | None:
        row = await self._pool.fetchrow(
            "SELECT embedding::text AS vec, metadata FROM embeddings "
            "WHERE tenant_id = %s AND collection = %s AND item_id = %s",
            (tenant_id, collection, item_id),
        )
        if row is None:
            return None
        raw = str(row["vec"]).strip("[]")
        vector = [float(x) for x in raw.split(",")] if raw else []
        return vector, dict(row["metadata"] or {})

    async def delete(self, collection: str, item_id: str, *, tenant_id: str) -> None:
        await self._pool.execute(
            "DELETE FROM embeddings WHERE tenant_id = %s AND collection = %s AND item_id = %s",
            (tenant_id, collection, item_id),
        )

    async def count(self, collection: str, *, tenant_id: str) -> int:
        value = await self._pool.fetchval(
            "SELECT count(*) FROM embeddings WHERE tenant_id = %s AND collection = %s",
            (tenant_id, collection),
        )
        return int(value or 0)

    async def ping(self) -> bool:
        return await self._pool.ping()

    async def close(self) -> None:
        await self._pool.close()
