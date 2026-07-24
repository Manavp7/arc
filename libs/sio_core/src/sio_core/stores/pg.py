"""Async Postgres access: one pooled helper shared by every SQL-backed adapter."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..errors import DependencyMissing, StoreError
from ..telemetry import get_logger

log = get_logger("sio.pg")


class PgPool:
    """Thin async wrapper over ``psycopg_pool.AsyncConnectionPool``.

    Deliberately thin: SIO's SQL lives in ``infra/postgres/*.sql`` and in the adapters, not in
    an ORM. Spatial predicates (PostGIS) and vector operators (pgvector) are the whole point
    of using Postgres here, and both are far clearer written directly.
    """

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 8) -> None:
        try:
            from psycopg_pool import AsyncConnectionPool
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise DependencyMissing("psycopg[binary,pool]", "PgPool") from exc
        self._dsn = dsn
        self._pool: Any = AsyncConnectionPool(
            dsn, min_size=min_size, max_size=max_size, open=False, kwargs={"autocommit": True}
        )
        self._opened = False

    async def open(self) -> None:
        if not self._opened:
            await self._pool.open(wait=True, timeout=30)
            self._opened = True

    async def close(self) -> None:
        if self._opened:
            await self._pool.close()
            self._opened = False

    async def _conn(self) -> Any:
        await self.open()
        return self._pool.connection()

    async def execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        """Run a statement, returning the affected row count."""
        try:
            async with await self._conn() as conn, conn.cursor() as cur:
                await cur.execute(sql, params)
                return cur.rowcount
        except Exception as exc:
            raise StoreError(f"postgres execute failed: {exc}") from exc

    async def execute_many(self, sql: str, rows: Sequence[Sequence[Any]]) -> int:
        if not rows:
            return 0
        try:
            async with await self._conn() as conn, conn.cursor() as cur:
                await cur.executemany(sql, rows)
                return cur.rowcount
        except Exception as exc:
            raise StoreError(f"postgres executemany failed: {exc}") from exc

    async def fetch(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        try:
            from psycopg.rows import dict_row

            async with await self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                return list(await cur.fetchall())
        except Exception as exc:
            raise StoreError(f"postgres fetch failed: {exc}") from exc

    async def fetchrow(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> dict[str, Any] | None:
        rows = await self.fetch(sql, params)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        row = await self.fetchrow(sql, params)
        if row is None:
            return None
        return next(iter(row.values()), None)

    async def ping(self) -> bool:
        try:
            return await self.fetchval("SELECT 1") == 1
        except Exception:
            return False

    async def extensions(self) -> set[str]:
        """Installed extensions — ``just doctor`` uses this to prove PostGIS/pgvector are live."""
        rows = await self.fetch("SELECT extname FROM pg_extension")
        return {r["extname"] for r in rows}
