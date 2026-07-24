"""One contract suite, two adapters.

Every test here runs against ``MemoryBus`` always and ``RedisStreamBus`` when
``SIO_TEST_INFRA=1``. That is the mechanism that keeps the in-memory adapter honest: if the
memory bus and Redis ever diverge on delivery semantics, this file fails rather than a
production incident later.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sio_schemas import BusMessage, Detection, Event, EventType, Topic, new_id, utc_now

INFRA = os.environ.get("SIO_TEST_INFRA") == "1"


def make_detection(label: str = "truck", confidence: float = 0.9) -> Detection:
    return Detection(
        observation_id=new_id("obs"),
        **{"class": label},
        confidence=confidence,
        source_id="cam-gate-a",
    )


@pytest.fixture(params=["memory", pytest.param("redis", marks=pytest.mark.infra)])
async def bus(request: pytest.FixtureRequest) -> AsyncIterator[object]:
    """Yield each bus adapter in turn, isolated to a unique topic namespace."""
    if request.param == "memory":
        from sio_core.bus.memory import MemoryBus

        adapter: object = MemoryBus()
        yield adapter
        await adapter.close()  # type: ignore[attr-defined]
    else:
        if not INFRA:
            pytest.skip("redis contract tests need SIO_TEST_INFRA=1")
        from sio_core.bus.redis_bus import RedisStreamBus

        url = os.environ.get("SIO_REDIS_URL", "redis://127.0.0.1:6379/0")
        adapter = RedisStreamBus(url, claim_idle_ms=200, max_retries=3)
        if not await adapter.ping():  # type: ignore[attr-defined]
            pytest.skip(f"redis not reachable at {url}")
        yield adapter
        await adapter.close()  # type: ignore[attr-defined]


@pytest.fixture
def topic() -> str:
    """A unique topic per test, so Redis runs are hermetic and repeatable."""
    return f"test.{new_id('tp').lower()}"


async def consume_n(bus: object, topic: str, group: str, count: int, *, ack: bool = True) -> list:
    """Read ``count`` messages, optionally acking them."""
    collected: list[BusMessage] = []
    agen = bus.consume([topic], group=group, consumer="c1", block_ms=200, batch=count)  # type: ignore[attr-defined]
    try:
        async for message in agen:
            collected.append(message)
            if ack and message.stream_id:
                await bus.ack(topic, group, message.stream_id)  # type: ignore[attr-defined]
            if len(collected) >= count:
                break
    finally:
        await agen.aclose()
    return collected


# ------------------------------------------------------------------ round trip
async def test_publish_and_consume_round_trip(bus: object, topic: str) -> None:
    detection = make_detection()
    await bus.publish(topic, detection, producer="perception")  # type: ignore[attr-defined]

    received = await consume_n(bus, topic, "g1", 1)
    assert len(received) == 1
    message = received[0]
    assert message.topic == topic
    assert message.kind == "Detection"
    assert message.producer == "perception"
    assert message.stream_id, "adapters must report the stream id so the caller can ack"
    decoded = message.decode(Detection)
    assert decoded.id == detection.id
    assert decoded.class_name == "truck"


async def test_trace_id_survives_the_wire(bus: object, topic: str) -> None:
    """The whole explainability story depends on this."""
    detection = make_detection()
    await bus.publish(topic, detection)  # type: ignore[attr-defined]
    [message] = await consume_n(bus, topic, "g1", 1)
    assert message.trace_id == detection.trace_id
    assert message.decode(Detection).trace_id == detection.trace_id


async def test_explicit_trace_id_overrides_the_payload(bus: object, topic: str) -> None:
    await bus.publish(topic, make_detection(), trace_id="trc_forced")  # type: ignore[attr-defined]
    [message] = await consume_n(bus, topic, "g1", 1)
    assert message.trace_id == "trc_forced"


async def test_message_order_is_preserved(bus: object, topic: str) -> None:
    labels = ["truck", "person", "forklift", "drone"]
    for label in labels:
        await bus.publish(topic, make_detection(label))  # type: ignore[attr-defined]
    received = await consume_n(bus, topic, "g1", len(labels))
    assert [m.decode(Detection).class_name for m in received] == labels


# --------------------------------------------------------------- consumer groups
async def test_groups_are_independent(bus: object, topic: str) -> None:
    """Adding a consumer must never steal messages from an existing service."""
    await bus.publish(topic, make_detection())  # type: ignore[attr-defined]
    first = await consume_n(bus, topic, "tracking", 1)
    second = await consume_n(bus, topic, "fusion", 1)
    assert len(first) == len(second) == 1
    assert first[0].id == second[0].id


async def test_ensure_group_is_idempotent(bus: object, topic: str) -> None:
    await bus.ensure_group(topic, "g1")  # type: ignore[attr-defined]
    await bus.ensure_group(topic, "g1")  # type: ignore[attr-defined]
    await bus.publish(topic, make_detection())  # type: ignore[attr-defined]
    assert len(await consume_n(bus, topic, "g1", 1)) == 1


async def test_consumer_can_start_before_any_producer(bus: object, topic: str) -> None:
    """Service start order must not matter on a laptop."""
    await bus.ensure_group(topic, "g1")  # type: ignore[attr-defined]
    await bus.publish(topic, make_detection())  # type: ignore[attr-defined]
    assert len(await consume_n(bus, topic, "g1", 1)) == 1


# ------------------------------------------------------------- at-least-once
async def test_unacked_messages_stay_pending(bus: object, topic: str) -> None:
    """At-least-once: without an ack the message is still owed to the group."""
    await bus.publish(topic, make_detection())  # type: ignore[attr-defined]
    await consume_n(bus, topic, "g1", 1, ack=False)
    assert await bus.lag(topic, "g1") >= 1  # type: ignore[attr-defined]


async def test_ack_clears_the_lag(bus: object, topic: str) -> None:
    await bus.publish(topic, make_detection())  # type: ignore[attr-defined]
    await consume_n(bus, topic, "g1", 1, ack=True)
    assert await bus.lag(topic, "g1") == 0  # type: ignore[attr-defined]


async def test_lag_reflects_backlog(bus: object, topic: str) -> None:
    for _ in range(5):
        await bus.publish(topic, make_detection())  # type: ignore[attr-defined]
    await bus.ensure_group(topic, "g1")  # type: ignore[attr-defined]
    lag = await bus.lag(topic, "g1")  # type: ignore[attr-defined]
    assert lag == 5, f"expected a backlog of 5, got {lag}"


async def test_delivery_count_is_reported(bus: object, topic: str) -> None:
    await bus.publish(topic, make_detection())  # type: ignore[attr-defined]
    [message] = await consume_n(bus, topic, "g1", 1, ack=False)
    assert message.delivery_count >= 1


# -------------------------------------------------------------- dead lettering
async def test_dead_letter_moves_the_message_aside(bus: object, topic: str) -> None:
    """A poison message must not be able to wedge a stream forever."""
    await bus.publish(topic, make_detection())  # type: ignore[attr-defined]
    [message] = await consume_n(bus, topic, "g1", 1)
    await bus.dead_letter(message, "handler exploded")  # type: ignore[attr-defined]

    dlq = await consume_n(bus, f"dlq.{topic}", "dlq-reader", 1)
    assert len(dlq) == 1
    payload = dlq[0].payload
    assert payload["reason"] == "handler exploded"
    assert payload["original_id"] == message.id
    assert payload["original"]["class"] == "truck", "the original payload must be preserved"


# --------------------------------------------------------------------- replay
async def test_read_range_returns_history_in_order(bus: object, topic: str) -> None:
    for label in ("truck", "person"):
        await bus.publish(topic, make_detection(label))  # type: ignore[attr-defined]
    history = await bus.read_range(topic)  # type: ignore[attr-defined]
    assert [m.decode(Detection).class_name for m in history] == ["truck", "person"]


async def test_read_range_respects_a_time_window(bus: object, topic: str) -> None:
    """Timeline replay reads a window, not the whole stream."""
    await bus.publish(topic, make_detection())  # type: ignore[attr-defined]
    future = utc_now() + timedelta(minutes=5)
    assert await bus.read_range(topic, start=future) == []  # type: ignore[attr-defined]
    past = utc_now() - timedelta(minutes=5)
    assert len(await bus.read_range(topic, start=past)) == 1  # type: ignore[attr-defined]


async def test_read_range_honours_limit(bus: object, topic: str) -> None:
    for _ in range(6):
        await bus.publish(topic, make_detection())  # type: ignore[attr-defined]
    assert len(await bus.read_range(topic, limit=2)) == 2  # type: ignore[attr-defined]


async def test_read_range_does_not_disturb_consumers(bus: object, topic: str) -> None:
    await bus.publish(topic, make_detection())  # type: ignore[attr-defined]
    await bus.read_range(topic)  # type: ignore[attr-defined]
    assert len(await consume_n(bus, topic, "g1", 1)) == 1


# ------------------------------------------------------------------ maintenance
async def test_length_and_trim(bus: object, topic: str) -> None:
    for _ in range(4):
        await bus.publish(topic, make_detection())  # type: ignore[attr-defined]
    assert await bus.length(topic) == 4  # type: ignore[attr-defined]
    await bus.trim(topic, 2)  # type: ignore[attr-defined]
    assert await bus.length(topic) <= 4  # approximate trimming is allowed


async def test_ping(bus: object) -> None:
    assert await bus.ping() is True  # type: ignore[attr-defined]


async def test_publish_message_accepts_a_prebuilt_envelope(bus: object, topic: str) -> None:
    envelope = BusMessage.of(topic, Event(type=EventType.FIRE_DETECTED), producer="events")
    await bus.publish_message(envelope)  # type: ignore[attr-defined]
    [message] = await consume_n(bus, topic, "g1", 1)
    assert message.id == envelope.id
    assert message.decode(Event).type is EventType.FIRE_DETECTED


async def test_topics_enum_values_are_usable_directly(bus: object) -> None:
    """Services pass ``Topic`` members, not strings; adapters must accept both."""
    await bus.publish(Topic.EVENTS, Event(type=EventType.ZONE_BREACH))  # type: ignore[attr-defined]
    history = await bus.read_range(Topic.EVENTS)  # type: ignore[attr-defined]
    assert history and history[-1].topic == "events"
