"""Redis Streams bus adapter — the default nervous system (PRD §9.1).

Chosen over Kafka for local-first operation: Redis is one Homebrew formula, one process, and
gives us consumer groups, replayable history and per-stream trimming. The `KafkaBus` swap
(PRD §9.3) exists behind the same port for scale-out.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any

from sio_schemas import BusMessage, SioModel

from ..errors import BusError
from ..telemetry import get_logger
from .codec import decode, encode, ts_to_stream_id

log = get_logger("sio.bus.redis")


class RedisStreamBus:
    """At-least-once streaming over Redis Streams.

    Delivery guarantees come from consumer groups plus explicit acks. Two extra behaviours
    matter in practice and are implemented here rather than left to each service:

    - **stalled-consumer recovery**: ``XAUTOCLAIM`` reclaims messages whose owner died, so a
      crashed service does not strand its in-flight work;
    - **poison-message containment**: after ``max_retries`` deliveries a message is moved to
      ``dlq.<topic>`` and acked, so one bad payload cannot wedge a stream forever.
    """

    def __init__(
        self,
        url: str,
        *,
        maxlen: int = 100_000,
        block_ms: int = 2_000,
        batch: int = 64,
        claim_idle_ms: int = 60_000,
        max_retries: int = 5,
    ) -> None:
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover - dependency is declared
            from ..errors import DependencyMissing

            raise DependencyMissing("redis", "RedisStreamBus") from exc

        self._redis: Any = Redis.from_url(url, decode_responses=True)
        self._url = url
        self._maxlen = maxlen
        self._block_ms = block_ms
        self._batch = batch
        self._claim_idle_ms = claim_idle_ms
        self._max_retries = max_retries
        self._known_groups: set[tuple[str, str]] = set()

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
        try:
            return str(
                await self._redis.xadd(
                    str(message.topic),
                    encode(message),
                    maxlen=self._maxlen,
                    approximate=True,
                )
            )
        except Exception as exc:
            raise BusError(f"publish to {message.topic} failed: {exc}") from exc

    # ------------------------------------------------------------------ consume
    async def ensure_group(self, topic: str, group: str) -> None:
        key = (str(topic), group)
        if key in self._known_groups:
            return
        try:
            # mkstream so a consumer can start before the first producer exists — otherwise
            # service start order would matter, which on a laptop it never should.
            await self._redis.xgroup_create(str(topic), group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise BusError(f"could not create group {group} on {topic}: {exc}") from exc
        self._known_groups.add(key)

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
        block = block_ms or self._block_ms
        count = batch or self._batch
        claim_cursors = dict.fromkeys(names, "0-0")

        while True:
            # First, reclaim anything abandoned by a dead consumer.
            for topic in names:
                cursor, claimed, _ = await self._redis.xautoclaim(
                    topic,
                    group,
                    consumer,
                    min_idle_time=self._claim_idle_ms,
                    start_id=claim_cursors[topic],
                    count=count,
                )
                claim_cursors[topic] = cursor or "0-0"
                for stream_id, fields in claimed or []:
                    message = await self._prepare(topic, group, stream_id, fields)
                    if message is not None:
                        yield message

            try:
                response = await self._redis.xreadgroup(
                    group, consumer, dict.fromkeys(names, ">"), count=count, block=block
                )
            except Exception as exc:
                raise BusError(f"xreadgroup failed for {names}: {exc}") from exc

            for topic, entries in response or []:
                for stream_id, fields in entries:
                    message = await self._prepare(topic, group, stream_id, fields)
                    if message is not None:
                        yield message

    async def _prepare(
        self, topic: str, group: str, stream_id: str, fields: dict[str, str]
    ) -> BusMessage | None:
        """Decode an entry, dead-lettering poison messages and over-retried ones."""
        try:
            message = decode(fields, stream_id=stream_id)
        except Exception as exc:
            log.error("bus.undecodable", topic=topic, stream_id=stream_id, error=str(exc))
            await self._redis.xadd(
                f"dlq.{topic}",
                {"raw": str(fields), "reason": f"decode failed: {exc}"},
                maxlen=self._maxlen,
                approximate=True,
            )
            await self.ack(topic, group, stream_id)
            return None

        message.delivery_count = await self._delivery_count(topic, group, stream_id)
        if message.delivery_count > self._max_retries:
            log.error(
                "bus.dead_letter",
                topic=topic,
                stream_id=stream_id,
                deliveries=message.delivery_count,
                trace_id=message.trace_id,
            )
            await self.dead_letter(message, f"exceeded {self._max_retries} deliveries")
            await self.ack(topic, group, stream_id)
            return None
        return message

    async def _delivery_count(self, topic: str, group: str, stream_id: str) -> int:
        """How many times this message has been delivered, per Redis' own bookkeeping."""
        try:
            pending = await self._redis.xpending_range(
                topic, group, min=stream_id, max=stream_id, count=1
            )
        except Exception:
            return 1
        if not pending:
            return 1
        entry = pending[0]
        value = entry.get("times_delivered") if isinstance(entry, dict) else None
        return int(value) if value else 1

    async def ack(self, topic: str, group: str, stream_id: str) -> None:
        await self._redis.xack(str(topic), group, stream_id)

    async def nack(self, topic: str, group: str, stream_id: str) -> None:
        """Explicitly decline a message.

        Redis has no negative acknowledgement: not acking *is* the nack. The message stays in
        the group's pending entries list and is reclaimed by ``XAUTOCLAIM`` once it has been
        idle for ``claim_idle_ms``. Implemented as a no-op so callers can be adapter-agnostic.
        """
        return None

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
        """Follow topics with ``XREAD`` — no group, no cursor, no acks.

        ``$`` means "only what arrives after this call", which is exactly what an SSE client wants.
        Using a consumer group here would either replay the whole stream to every browser or move a
        real consumer's cursor.
        """
        names = [str(t) for t in topics]
        block = block_ms or self._block_ms
        count = batch or self._batch
        if start is None:
            cursors = dict.fromkeys(names, "$")
        else:
            cursors = dict.fromkeys(names, str(int(start.timestamp() * 1000) - 1))

        while True:
            try:
                response = await self._redis.xread(cursors, count=count, block=block)
            except Exception as exc:
                raise BusError(f"xread failed for {names}: {exc}") from exc
            for topic, entries in response or []:
                for stream_id, fields in entries:
                    cursors[topic] = stream_id
                    try:
                        yield decode(fields, stream_id=stream_id)
                    except Exception as exc:
                        log.warning(
                            "bus.tail_skip", topic=topic, stream_id=stream_id, error=str(exc)
                        )

    async def read_range(
        self,
        topic: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 1000,
    ) -> list[BusMessage]:
        lo = ts_to_stream_id(start) if start else "-"
        hi = ts_to_stream_id(end) if end else "+"
        entries = await self._redis.xrange(str(topic), min=lo, max=hi, count=limit)
        out: list[BusMessage] = []
        for stream_id, fields in entries or []:
            try:
                out.append(decode(fields, stream_id=stream_id))
            except Exception as exc:
                log.warning("bus.replay_skip", topic=topic, stream_id=stream_id, error=str(exc))
        return out

    async def lag(self, topic: str, group: str) -> int:
        try:
            groups = await self._redis.xinfo_groups(str(topic))
        except Exception:
            return 0
        for info in groups or []:
            if info.get("name") == group:
                # `lag` is exact when available; `pending` is the portion in flight.
                lag = info.get("lag")
                pending = int(info.get("pending", 0) or 0)
                return int(lag) + pending if lag is not None else pending
        return 0

    async def trim(self, topic: str, maxlen: int) -> None:
        await self._redis.xtrim(str(topic), maxlen=maxlen, approximate=True)

    async def length(self, topic: str) -> int:
        try:
            return int(await self._redis.xlen(str(topic)))
        except Exception:
            return 0

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False

    async def close(self) -> None:
        await self._redis.aclose()
