"""Serialize a tenant's alert mutations inside one PostgreSQL transaction.

All read/modify/write paths use the same transaction-scoped advisory lock. The
connection is task-local, so HTTP requests and the consumer/timer cannot share a
transaction accidentally. Network publication happens only after commit.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from sio_core import PgPool


class ConnectionPool:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    async def execute(self, sql: str, params: Any = None) -> int:
        async with self.connection.cursor() as cursor:
            await cursor.execute(sql, params)
            return cursor.rowcount

    async def fetch(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        async with PgPool.dict_cursor(self.connection) as cursor:
            await cursor.execute(sql, params)
            return list(await cursor.fetchall())

    async def fetchrow(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        rows = await self.fetch(sql, params)
        return rows[0] if rows else None


class AlertTransactions:
    def __init__(self) -> None:
        self.current: ContextVar[ConnectionPool | None] = ContextVar(
            "alert_transaction", default=None
        )
        self.publications: ContextVar[list[Any] | None] = ContextVar(
            "alert_publications", default=None
        )

    @asynccontextmanager
    async def mutation(self, pool: PgPool, tenant: str) -> AsyncIterator[list[Any]]:
        active = self.current.get()
        if active is not None:
            yield self.publications.get() or []
            return
        pending: list[Any] = []
        async with await pool._conn() as connection, connection.transaction():
            db = ConnectionPool(connection)
            # Hash collisions merely serialize unrelated tenants; they cannot cross tenant filters.
            await db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("alerts:" + tenant,)
            )
            token = self.current.set(db)
            publications = self.publications.set(pending)
            try:
                yield pending
            finally:
                self.current.reset(token)
                self.publications.reset(publications)
