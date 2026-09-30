"""PostgreSQL session claims shared by cooperating API processes on one media store.

Session locks have no timeout-based lease theft: a paused native decoder keeps its
claim until it finishes or its database session dies. Writes use the owning session,
so a worker whose claim connection is lost cannot publish stale completion state.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sio_core import PgPool

_held: ContextVar[tuple] = ContextVar("media_database_claims", default=())


class WorkerPresence:
    """A session-owned liveness claim, also covering jobs waiting for the processor."""

    def __init__(self, store):
        self.store = store
        self.identifier = "media-worker-" + uuid.uuid4().hex
        self.connection = None
        self.started = False
        self.guard = asyncio.Lock()

    async def start(self):
        async with self.guard:
            if self.started:
                try:
                    if self.connection:
                        await self.connection.execute("SELECT 1")
                    return self.identifier
                except Exception:
                    await self.connection.close()
                    self.connection = None
                    self.started = False
                    # A replacement session must never claim the old worker's queued records.
                    self.identifier = "media-worker-" + uuid.uuid4().hex
            dsn = getattr(getattr(self.store, "pool", None), "_dsn", None)
            if isinstance(dsn, str):
                self.connection = await PgPool.dedicated_connection(dsn)
                await self.connection.execute(
                    "SELECT pg_advisory_lock(hashtextextended(%s, 0))", (self.identifier,)
                )
            else:
                if not hasattr(self.store, "_live_media_workers"):
                    self.store._live_media_workers = set()
                self.store._live_media_workers.add(self.identifier)
            self.started = True
            return self.identifier

    async def close(self):
        if self.connection:
            await self.connection.close()
        elif hasattr(self.store, "_live_media_workers"):
            self.store._live_media_workers.discard(self.identifier)
        self.started = False

    async def alive(self, identifier):
        if not identifier:
            return False
        dsn = getattr(getattr(self.store, "pool", None), "_dsn", None)
        if not isinstance(dsn, str):
            return identifier in getattr(self.store, "_live_media_workers", set())
        async with await PgPool.dedicated_connection(dsn) as conn:
            cursor = await conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (identifier,)
            )
            available = (await cursor.fetchone())[0]
            # Closing this dedicated connection releases the probe lock if acquired.
            return not available


class DatabaseLock:
    def __init__(self, dsn, name):
        self.dsn = dsn
        self.key = int.from_bytes(
            hashlib.sha256(json.dumps(name).encode()).digest()[:8], "big", signed=True
        )
        self.local = asyncio.Lock()
        self.connection = None
        self.token = None

    def locked(self):
        return self.local.locked()

    async def acquire(self, *, wait=True):
        if not wait and self.local.locked():
            return False
        await self.local.acquire()
        try:
            self.connection = await PgPool.dedicated_connection(self.dsn)
            command = "SELECT pg_advisory_lock(%s)" if wait else "SELECT pg_try_advisory_lock(%s)"
            cursor = await self.connection.execute(command, (self.key,))
            result = await cursor.fetchone()
            if not wait and not result[0]:
                await self.connection.close()
                self.connection = None
                self.local.release()
                return False
            self.token = _held.set((*_held.get(), (asyncio.current_task(), self)))
            return True
        except BaseException:
            if self.connection:
                await self.connection.close()
                self.connection = None
            self.local.release()
            raise

    async def release(self):
        try:
            if self.connection:
                await self.connection.close()
        finally:
            self.connection = None
            if self.token is not None:
                _held.reset(self.token)
                self.token = None
            self.local.release()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *args):
        await self.release()


def shared_lock(store, *name):
    if not hasattr(store, "_media_shared_locks"):
        store._media_shared_locks = {}
    key = tuple(name)
    if key not in store._media_shared_locks:
        dsn = getattr(getattr(store, "pool", None), "_dsn", None)
        store._media_shared_locks[key] = (
            DatabaseLock(dsn, key) if isinstance(dsn, str) else asyncio.Lock()
        )
    return store._media_shared_locks[key]


@asynccontextmanager
async def claim(store, *name, wait=False):
    lock = shared_lock(store, *name)
    if isinstance(lock, DatabaseLock):
        acquired = await lock.acquire(wait=wait)
    else:
        acquired = False if not wait and lock.locked() else await lock.acquire()
    try:
        yield acquired
    finally:
        if acquired:
            if isinstance(lock, DatabaseLock):
                await lock.release()
            else:
                lock.release()


async def coordinated_fetchrow(pool, sql, params):
    """Fence document publication through this task's outermost live claim session."""
    locks = [lock for owner, lock in _held.get() if owner is asyncio.current_task()]
    if not locks:
        return await pool.fetchrow(sql, params)
    for lock in locks:
        # Fail before persisting if any nested lifecycle claim was lost.
        await lock.connection.execute("SELECT 1")
    async with PgPool.dict_cursor(locks[0].connection) as cursor:
        await cursor.execute(sql, params)
        return await cursor.fetchone()
