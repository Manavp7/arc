"""In-process bus adapter.

Not a toy: it implements the same at-least-once, consumer-group, dead-letter and replay
semantics as the Redis adapter, and both are held to one shared contract suite. That is what
lets the entire unit-test ring — and `just dev-lite` on a laptop with nothing installed —
run without infrastructure.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sio_schemas import BusMessage, SioModel

from ..errors import BusError
from .codec import decode, encode


@dataclass
class _Entry:
    stream_id: str
    fields: dict[str, str]
    ts_ms: int


@dataclass
class _Group:
    cursor: int = 0
    pending: dict[str, int] = field(default_factory=dict)  # stream_id -> delivery count


class MemoryBus:
    """Bus backed by in-process lists. Safe for concurrent producers/consumers in one loop."""

    def __init__(self, *, maxlen: int = 100_000) -> None:
        self._streams: dict[str, list[_Entry]] = {}
        self._groups: dict[tuple[str, str], _Group] = {}
        self._seq = 0
        self._maxlen = maxlen
        self._wakeup: dict[str, asyncio.Event] = {}
        self._closed = False

    # ---------------------------------------------------------------- internals
    def _event(self, topic: str) -> asyncio.Event:
        return self._wakeup.setdefault(topic, asyncio.Event())

    def _next_id(self, ts_ms: int) -> str:
        self._seq += 1
        return f"{ts_ms}-{self._seq}"

    def _stream(self, topic: str) -> list[_Entry]:
        return self._streams.setdefault(topic, [])

    # ------------------------------------------------------------------ publish
    async def publish(
        self,
        topic: str,
        model: SioModel,
        *,
        producer: str = "unknown",
        trace_id: str | None = None,
    ) -> str:
        return await self.publish_message(
            BusMessage.of(str(topic), model, producer=producer, trace_id=trace_id)
        )

    async def publish_message(self, message: BusMessage) -> str:
        if self._closed:
            raise BusError("bus is closed")
        topic = str(message.topic)
        ts_ms = int(message.ts.timestamp() * 1000)
        entry = _Entry(stream_id=self._next_id(ts_ms), fields=encode(message), ts_ms=ts_ms)
        stream = self._stream(topic)
        stream.append(entry)
        if len(stream) > self._maxlen:
            trimmed = len(stream) - self._maxlen
            del stream[:trimmed]
            # Keep group cursors pointing at the same logical position after a trim.
            for (t, _), group in self._groups.items():
                if t == topic:
                    group.cursor = max(0, group.cursor - trimmed)
        self._event(topic).set()
        return entry.stream_id

    # ------------------------------------------------------------------ consume
    async def ensure_group(self, topic: str, group: str) -> None:
        """Create the group if absent, positioned at the *start* of the stream.

        Matches ``XGROUP CREATE … id=0`` in the Redis adapter: a service that boots after a
        producer still sees everything already published, so start order never costs data.
        Unbounded replay is prevented by ``MAXLEN`` trimming, not by skipping history.
        """
        self._groups.setdefault((str(topic), group), _Group(cursor=0))

    async def consume(
        self,
        topics: Sequence[str],
        *,
        group: str,
        consumer: str,
        block_ms: int | None = None,
        batch: int | None = None,
    ) -> AsyncIterator[BusMessage]:
        names = [str(t) for t in topics]
        for topic in names:
            await self.ensure_group(topic, group)
        block = (block_ms or 500) / 1000.0
        limit = batch or 64

        while not self._closed:
            delivered = 0
            for topic in names:
                state = self._groups[(topic, group)]
                stream = self._stream(topic)
                while state.cursor < len(stream) and delivered < limit:
                    entry = stream[state.cursor]
                    state.cursor += 1
                    count = state.pending.get(entry.stream_id, 0) + 1
                    state.pending[entry.stream_id] = count
                    delivered += 1
                    yield decode(entry.fields, stream_id=entry.stream_id, delivery_count=count)
            if delivered == 0:
                # Nothing to deliver: sleep until a producer wakes us or the block timeout
                # expires, so an idle consumer costs no CPU. Periodic service work belongs in
                # SioService.tick(), not in a heartbeat message, so this yields nothing.
                for topic in names:
                    self._event(topic).clear()
                waiters = [asyncio.create_task(self._event(t).wait()) for t in names]
                try:
                    await asyncio.wait(waiters, timeout=block, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in waiters:
                        task.cancel()

    async def ack(self, topic: str, group: str, stream_id: str) -> None:
        state = self._groups.get((str(topic), group))
        if state:
            state.pending.pop(stream_id, None)

    async def nack(self, topic: str, group: str, stream_id: str) -> None:
        """Return a message for redelivery by rewinding the cursor to it."""
        state = self._groups.get((str(topic), group))
        if not state:
            return
        stream = self._stream(str(topic))
        for index, entry in enumerate(stream):
            if entry.stream_id == stream_id:
                state.cursor = min(state.cursor, index)
                self._event(str(topic)).set()
                return

    async def dead_letter(self, message: BusMessage, reason: str) -> None:
        dlq = BusMessage(
            topic=f"dlq.{message.topic}",
            kind=message.kind,
            producer=message.producer,
            tenant_id=message.tenant_id,
            trace_id=message.trace_id,
            payload={"original": message.payload, "reason": reason, "original_id": message.id},
        )
        await self.publish_message(dlq)

    async def tail(
        self,
        topics: Sequence[str],
        *,
        start: datetime | None = None,
        block_ms: int | None = None,
        batch: int | None = None,
    ) -> AsyncIterator[BusMessage]:
        """Follow topics from now (or from ``start``) without a consumer group."""
        names = [str(t) for t in topics]
        block = (block_ms or 500) / 1000.0
        limit = batch or 64
        # Start at the current end of each stream unless a time is given: an observer wants what
        # happens next, not the backlog.
        cursors: dict[str, int] = {}
        for topic in names:
            if start is None:
                cursors[topic] = len(self._stream(topic))
            else:
                lo = int(start.timestamp() * 1000)
                stream = self._stream(topic)
                cursors[topic] = next(
                    (i for i, entry in enumerate(stream) if entry.ts_ms >= lo), len(stream)
                )

        while not self._closed:
            delivered = 0
            for topic in names:
                stream = self._stream(topic)
                while cursors[topic] < len(stream) and delivered < limit:
                    entry = stream[cursors[topic]]
                    cursors[topic] += 1
                    delivered += 1
                    yield decode(entry.fields, stream_id=entry.stream_id)
            if delivered == 0:
                for topic in names:
                    self._event(topic).clear()
                waiters = [asyncio.create_task(self._event(t).wait()) for t in names]
                try:
                    await asyncio.wait(waiters, timeout=block, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in waiters:
                        task.cancel()

    async def read_range(
        self,
        topic: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 1000,
    ) -> list[BusMessage]:
        lo = int(start.timestamp() * 1000) if start else None
        hi = int(end.timestamp() * 1000) if end else None
        out: list[BusMessage] = []
        for entry in self._stream(str(topic)):
            if lo is not None and entry.ts_ms < lo:
                continue
            if hi is not None and entry.ts_ms > hi:
                continue
            out.append(decode(entry.fields, stream_id=entry.stream_id))
            if len(out) >= limit:
                break
        return out

    async def lag(self, topic: str, group: str) -> int:
        state = self._groups.get((str(topic), group))
        if state is None:
            return len(self._stream(str(topic)))
        return max(0, len(self._stream(str(topic))) - state.cursor) + len(state.pending)

    async def trim(self, topic: str, maxlen: int) -> None:
        stream = self._stream(str(topic))
        if len(stream) > maxlen:
            del stream[: len(stream) - maxlen]

    async def length(self, topic: str) -> int:
        return len(self._stream(str(topic)))

    async def ping(self) -> bool:
        return not self._closed

    async def close(self) -> None:
        self._closed = True
        for event in self._wakeup.values():
            event.set()
