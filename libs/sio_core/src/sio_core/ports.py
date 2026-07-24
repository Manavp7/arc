"""Ports: the Protocols that define every swappable seam in SIO.

A *port* is an interface; an *adapter* implements it. Services depend only on the port and
ask :mod:`sio_core.registry` for an implementation, so replacing Redis with Kafka, or a
CPU ONNX detector with DeepStream, is an environment-variable change rather than a rewrite
(PRD §9.3). ``tests/unit/test_architecture.py`` enforces that services never import an
adapter module directly.

Compute-side ports (``Detector``, ``Tracker``, ``Embedder``, ``Forecaster``, ``LLM``,
``PolicyEngine``, ``AuthProvider``, ``WorkflowRunner``) are declared alongside the phase that
first needs them, so their signatures are designed against a real caller rather than guessed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from sio_schemas import BusMessage, Entity, Relationship, SioModel


@runtime_checkable
class Closeable(Protocol):
    async def close(self) -> None: ...


@runtime_checkable
class Pingable(Protocol):
    async def ping(self) -> bool:
        """Cheap liveness probe used by ``/health`` and ``just doctor``."""
        ...


@runtime_checkable
class Bus(Pingable, Closeable, Protocol):
    """Append-only, at-least-once message streams with consumer groups.

    Semantics every adapter must honour:

    - **at-least-once**: a message is redelivered until acked, so consumers must be idempotent;
    - **per-group cursors**: adding a consumer group never steals messages from another;
    - **replayable**: :meth:`read_range` can re-read history, which is what the timeline
      engine and ``just demo`` replay rely on;
    - **dead-lettering**: after ``bus_max_retries`` deliveries a message moves aside rather
      than blocking the stream forever.
    """

    async def publish(
        self,
        topic: str,
        model: SioModel,
        *,
        producer: str = "unknown",
        trace_id: str | None = None,
    ) -> str:
        """Wrap ``model`` in a :class:`BusMessage` and publish it. Returns the stream id."""
        ...

    async def publish_message(self, message: BusMessage) -> str: ...

    async def ensure_group(self, topic: str, group: str) -> None:
        """Create the consumer group if absent. Idempotent."""
        ...

    def consume(
        self,
        topics: Sequence[str],
        *,
        group: str,
        consumer: str,
        block_ms: int | None = None,
        batch: int | None = None,
    ) -> AsyncIterator[BusMessage]:
        """Yield messages for ``group``, including ones reclaimed from stalled consumers."""
        ...

    async def ack(self, topic: str, group: str, stream_id: str) -> None: ...

    async def nack(self, topic: str, group: str, stream_id: str) -> None:
        """Decline a message so it is redelivered. May be a no-op where not-acking suffices."""
        ...

    async def dead_letter(self, message: BusMessage, reason: str) -> None: ...

    async def read_range(
        self,
        topic: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 1000,
    ) -> list[BusMessage]:
        """Read history for replay. Ordered oldest → newest."""
        ...

    def tail(
        self,
        topics: Sequence[str],
        *,
        start: datetime | None = None,
        block_ms: int | None = None,
        batch: int | None = None,
    ) -> AsyncIterator[BusMessage]:
        """Follow topics from *now* (or from ``start``), without consumer groups or acks.

        Distinct from :meth:`consume` on purpose. `consume` is for work that must not be lost:
        it uses a group, tracks a cursor and requires an ack. `tail` is for observers — the SSE
        fan-out to browsers, the timeline tail, `just demo` narration — where the only sane
        starting point is "whatever happens next". Tailing through a consumer group would either
        replay the entire stream to every browser that connects, or corrupt a real consumer's
        cursor.
        """
        ...

    async def lag(self, topic: str, group: str) -> int:
        """Messages pending for ``group``. The core backpressure signal."""
        ...

    async def trim(self, topic: str, maxlen: int) -> None: ...


@runtime_checkable
class GraphStore(Pingable, Closeable, Protocol):
    """The world model's entity/relationship graph.

    Relationships are **bitemporal**: closing one sets ``ts_valid_to`` instead of deleting,
    so :meth:`snapshot_at` can rebuild the graph as it stood at any instant (PRD M8/UC5).
    """

    async def upsert_entity(self, entity: Entity) -> None:
        """Insert or update an entity.

        **The merge contract, which every adapter must honour identically:** the store protects the
        *lifetime bounds only* — ``first_seen`` never moves later and ``last_seen`` never moves
        earlier, so a replayed or out-of-order message cannot shrink what is known about an entity.
        Everything else (attributes, provenance, state) belongs to the producer: fusion is the
        component that decides what an entity's attributes and provenance *are* (PRD M5), and a
        store that quietly merged them would compete with it.
        """
        ...

    async def upsert_entities(self, entities: Iterable[Entity]) -> int: ...

    async def get_entity(self, entity_id: str, *, tenant_id: str) -> Entity | None: ...

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
    ) -> list[Entity]: ...

    async def upsert_relationship(self, relationship: Relationship) -> None: ...

    async def close_relationship(
        self, relationship_id: str, *, tenant_id: str, ts: datetime
    ) -> None:
        """Close an open edge by stamping ``ts_valid_to``. Never deletes."""
        ...

    async def neighbors(
        self,
        entity_id: str,
        *,
        tenant_id: str,
        types: Sequence[str] | None = None,
        direction: str = "both",
        at: datetime | None = None,
        limit: int = 100,
    ) -> list[tuple[Relationship, Entity]]: ...

    async def path_between(
        self,
        from_id: str,
        to_id: str,
        *,
        tenant_id: str,
        max_hops: int = 4,
        at: datetime | None = None,
    ) -> list[Relationship]: ...

    async def snapshot_at(
        self, ts: datetime, *, tenant_id: str, limit: int = 1000
    ) -> tuple[list[Entity], list[Relationship]]:
        """Entities and edges valid at ``ts`` — the primitive behind timeline replay."""
        ...

    async def raw_query(
        self, query: str, params: Mapping[str, Any] | None = None, *, tenant_id: str
    ) -> list[dict[str, Any]]:
        """Execute a backend-native read query (Cypher or SQL).

        Exposed because the copilot's ``graph_query`` tool needs expressive traversal, and
        because the executed query text becomes evidence in the explanation. Adapters must
        reject writes.
        """
        ...

    async def counts(self, *, tenant_id: str) -> dict[str, int]: ...


@runtime_checkable
class VectorStore(Pingable, Closeable, Protocol):
    """Embedding storage and similarity search (semantic frame search, ReID, agent memory)."""

    dim: int

    async def upsert(
        self,
        collection: str,
        item_id: str,
        vector: Sequence[float],
        *,
        tenant_id: str,
        metadata: Mapping[str, Any] | None = None,
        ts: datetime | None = None,
    ) -> None: ...

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
        """Return ``(item_id, similarity, metadata)`` ordered by descending similarity."""
        ...

    async def get(
        self, collection: str, item_id: str, *, tenant_id: str
    ) -> tuple[list[float], dict[str, Any]] | None: ...

    async def delete(self, collection: str, item_id: str, *, tenant_id: str) -> None: ...

    async def count(self, collection: str, *, tenant_id: str) -> int: ...


@runtime_checkable
class BlobStore(Pingable, Closeable, Protocol):
    """Immutable object storage for raw media (frames, clips, audio, masks, tiles, reports)."""

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: Mapping[str, str] | None = None,
    ) -> str:
        """Store bytes and return the canonical key."""
        ...

    async def get(self, key: str) -> bytes: ...

    async def exists(self, key: str) -> bool: ...

    async def delete(self, key: str) -> None: ...

    async def list(self, prefix: str = "", *, limit: int = 1000) -> list[str]: ...

    def url_for(self, key: str) -> str:
        """A URL the UI can fetch. May be presigned, may be an API proxy path."""
        ...
