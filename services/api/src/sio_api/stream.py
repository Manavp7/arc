"""Live update fan-out: one bus tail per process, many browser clients.

A naive implementation gives every SSE connection its own bus subscription. With five operator
consoles open that is five Redis connections re-reading the same stream, and each one has to decide
independently where to start. Instead there is a single tail per process and an in-process
broadcast: connections are cheap, and every client sees exactly the same sequence.

Slow clients are dropped rather than allowed to apply backpressure to the tail. A browser on a bad
connection must not be able to slow down the live picture for everyone else, and a stale queue is
worse than a reconnect: SSE clients reconnect automatically and the console reloads its snapshot.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Sequence
from typing import Any

from sio_core import describe_error, get_logger
from sio_core.ports import Bus
from sio_schemas import BusMessage, Topic

log = get_logger("sio.api.stream")

DEFAULT_TOPICS: tuple[str, ...] = (
    str(Topic.ENTITIES),
    str(Topic.EVENTS),
    str(Topic.ALERTS),
    str(Topic.DECISIONS),
    str(Topic.FORECASTS),
    str(Topic.TRACKS),
)


class Subscriber:
    """One connected client."""

    __slots__ = ("dropped", "queue", "topics")

    def __init__(self, topics: Sequence[str] | None, maxsize: int = 500) -> None:
        self.queue: asyncio.Queue[BusMessage] = asyncio.Queue(maxsize=maxsize)
        self.topics = {str(t) for t in topics} if topics else None
        self.dropped = 0

    def wants(self, message: BusMessage) -> bool:
        return self.topics is None or str(message.topic) in self.topics

    def offer(self, message: BusMessage) -> None:
        if not self.wants(message):
            return
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            # Drop the oldest so a slow client keeps receiving *recent* state rather than a stale
            # backlog — for a live map, newer is strictly more useful.
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            self.dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(message)


class StreamHub:
    """Tails the bus once and broadcasts to every subscriber."""

    def __init__(self, bus: Bus, topics: Sequence[str] = DEFAULT_TOPICS) -> None:
        self.bus = bus
        self.topics = [str(t) for t in topics]
        self.subscribers: set[Subscriber] = set()
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self.forwarded = 0

    async def start(self) -> None:
        if self._task is None:
            self._stopping.clear()
            self._task = asyncio.create_task(self._pump(), name="api-stream-hub")
            log.info("stream.hub_started", topics=self.topics)

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _pump(self) -> None:
        while not self._stopping.is_set():
            try:
                async for message in self.bus.tail(self.topics, block_ms=1000):
                    self.forwarded += 1
                    for subscriber in list(self.subscribers):
                        subscriber.offer(message)
                    if self._stopping.is_set():
                        break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("stream.tail_failed", error=describe_error(exc))
                await asyncio.sleep(1.0)

    @contextlib.contextmanager
    def subscribe(self, topics: Sequence[str] | None = None) -> Any:
        subscriber = Subscriber(topics)
        self.subscribers.add(subscriber)
        log.debug("stream.subscribed", clients=len(self.subscribers))
        try:
            yield subscriber
        finally:
            self.subscribers.discard(subscriber)
            if subscriber.dropped:
                log.info("stream.client_lagged", dropped=subscriber.dropped)

    async def events(
        self, subscriber: Subscriber, *, keepalive_s: float = 15.0
    ) -> AsyncIterator[str]:
        """Server-Sent Events frames for one subscriber.

        The keepalive comment matters: proxies and load balancers close idle connections, and a
        quiet yard is a normal state, not a dead one.
        """
        yield ": connected\n\n"
        while True:
            try:
                message = await asyncio.wait_for(subscriber.queue.get(), timeout=keepalive_s)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            payload = message.model_dump_json(by_alias=True)
            yield f"id: {message.stream_id or message.id}\nevent: {message.kind}\ndata: {payload}\n\n"

    def stats(self) -> dict[str, Any]:
        return {
            "clients": len(self.subscribers),
            "forwarded": self.forwarded,
            "topics": self.topics,
            "dropped_total": sum(s.dropped for s in self.subscribers),
        }
