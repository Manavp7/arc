"""Tests for the ``SioService`` runtime.

The behaviours asserted here are the ones every service inherits and none re-implements:
idempotency under redelivery, dead-lettering of bad payloads, retry of transient failures,
trace propagation to downstream messages, and an honest ``/health``.
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sio_core import MessageContext, SioService
from sio_core.errors import ValidationFailed
from sio_schemas import BusMessage, Detection, Event, EventType, Topic, new_id


def a_detection(label: str = "truck") -> Detection:
    return Detection(
        observation_id=new_id("obs"),
        **{"class": label},
        confidence=0.9,
        source_id="cam-gate-a",
    )


class RecordingService(SioService):
    name = "recorder"
    subscribes = (Topic.DETECTIONS,)

    def __init__(self, settings, bus) -> None:  # type: ignore[no-untyped-def]
        super().__init__(settings, bus=bus)
        self.seen: list[Detection] = []

    async def on_message(self, message: BusMessage, ctx: MessageContext) -> None:
        detection = message.decode(Detection)
        self.seen.append(detection)
        await ctx.publish(Topic.EVENTS, Event(type=EventType.ENTITY_APPEARED))


class RejectingService(SioService):
    """Raises a domain error — a bad message, which must be dead-lettered immediately."""

    name = "rejecter"
    subscribes = (Topic.DETECTIONS,)

    async def on_message(self, message: BusMessage, ctx: MessageContext) -> None:
        raise ValidationFailed("payload makes no sense")


class FlakyService(SioService):
    """Raises an unexpected error — transient, so the message must stay unacked for retry."""

    name = "flaky"
    subscribes = (Topic.DETECTIONS,)
    attempts = 0

    async def on_message(self, message: BusMessage, ctx: MessageContext) -> None:
        type(self).attempts += 1
        raise RuntimeError("database blinked")


async def test_service_consumes_and_publishes_downstream(settings, memory_bus) -> None:
    service = RecordingService(settings, memory_bus)
    detection = a_detection()
    await memory_bus.publish(Topic.DETECTIONS, detection, producer="perception")

    handled = await service.drain(limit=5, timeout_s=2.0)

    assert handled == 1
    assert [d.id for d in service.seen] == [detection.id]
    downstream = await memory_bus.read_range(Topic.EVENTS)
    assert len(downstream) == 1
    assert downstream[0].producer == "recorder"


async def test_downstream_messages_inherit_the_trace_id(settings, memory_bus) -> None:
    """Without this, an explanation cannot link an event back to the frame that caused it."""
    service = RecordingService(settings, memory_bus)
    detection = a_detection()
    await memory_bus.publish(Topic.DETECTIONS, detection)

    await service.drain(limit=1, timeout_s=2.0)

    [event] = await memory_bus.read_range(Topic.EVENTS)
    assert event.trace_id == detection.trace_id


async def test_duplicate_delivery_is_handled_once(settings, memory_bus) -> None:
    service = RecordingService(settings, memory_bus)
    envelope = BusMessage.of(Topic.DETECTIONS, a_detection(), producer="perception")
    await memory_bus.publish_message(envelope)
    await memory_bus.publish_message(envelope)  # same message id: at-least-once redelivery

    handled = await service.drain(limit=5, timeout_s=2.0)

    assert handled == 2, "both deliveries are consumed"
    assert len(service.seen) == 1, "but the handler only ran once"


async def test_acked_messages_clear_lag(settings, memory_bus) -> None:
    service = RecordingService(settings, memory_bus)
    await memory_bus.publish(Topic.DETECTIONS, a_detection())
    await service.drain(limit=1, timeout_s=2.0)
    assert await memory_bus.lag(str(Topic.DETECTIONS), service.group) == 0


async def test_domain_errors_are_dead_lettered_not_retried(settings, memory_bus) -> None:
    service = RejectingService(settings, bus=memory_bus)
    await memory_bus.publish(Topic.DETECTIONS, a_detection())

    await service.drain(limit=1, timeout_s=2.0)

    dlq = await memory_bus.read_range(f"dlq.{Topic.DETECTIONS}")
    assert len(dlq) == 1
    assert "makes no sense" in dlq[0].payload["reason"]
    # Acked, so it will not come back around.
    assert await memory_bus.lag(str(Topic.DETECTIONS), service.group) == 0


async def test_unexpected_errors_leave_the_message_for_retry(settings, memory_bus) -> None:
    service = FlakyService(settings, bus=memory_bus)
    FlakyService.attempts = 0
    await memory_bus.publish(Topic.DETECTIONS, a_detection())

    await service.drain(limit=1, timeout_s=2.0)

    assert FlakyService.attempts == 1
    assert await memory_bus.lag(str(Topic.DETECTIONS), service.group) >= 1, (
        "a transient failure must not be acked away"
    )
    assert await memory_bus.read_range(f"dlq.{Topic.DETECTIONS}") == []


async def test_health_reports_adapters_lag_and_counters(settings, memory_bus) -> None:
    service = RecordingService(settings, memory_bus)
    await memory_bus.publish(Topic.DETECTIONS, a_detection())
    await service.drain(limit=1, timeout_s=2.0)

    health = await service.health()

    assert health.service == "recorder"
    assert health.status == "ok"
    assert health.checks["bus"] == "ok"
    assert health.consumed == 1
    assert health.produced == 1
    assert health.errors == 0
    assert health.adapters["bus"] == "memory"
    assert str(Topic.DETECTIONS) in health.lag
    assert health.schema_version


async def test_health_degrades_when_a_dependency_fails(settings, memory_bus) -> None:
    class BrokenDeps(RecordingService):
        async def health_checks(self) -> dict[str, str]:
            return {"neo4j": "error: connection refused"}

    service = BrokenDeps(settings, memory_bus)
    health = await service.health()
    assert health.status == "degraded"
    assert health.checks["neo4j"].startswith("error")


def test_health_and_metrics_endpoints_are_served(settings, memory_bus) -> None:
    service = RecordingService(settings, memory_bus)
    with TestClient(service.app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["service"] == "recorder"

        metrics = client.get("/metrics")
        assert metrics.status_code == 200
        assert "sio_up" in metrics.text


def test_services_can_add_their_own_routes(settings, memory_bus) -> None:
    class WithRoutes(RecordingService):
        def routes(self, app: FastAPI) -> None:
            @app.get("/custom")
            async def custom() -> dict[str, bool]:
                return {"ok": True}

    service = WithRoutes(settings, memory_bus)
    with TestClient(service.app) as client:
        assert client.get("/custom").json() == {"ok": True}


async def test_tick_runs_on_its_interval(settings, memory_bus) -> None:
    class Ticker(SioService):
        name = "ticker"
        tick_interval_s = 0.05
        ticks = 0

        async def tick(self) -> None:
            type(self).ticks += 1

    service = Ticker(settings, bus=memory_bus)
    task = asyncio.create_task(service._tick_forever())
    await asyncio.sleep(0.2)
    service.stop()
    await asyncio.wait_for(task, timeout=1.0)
    assert Ticker.ticks >= 2


async def test_a_failing_tick_does_not_kill_the_loop(settings, memory_bus) -> None:
    class BadTicker(SioService):
        name = "badticker"
        tick_interval_s = 0.05
        calls = 0

        async def tick(self) -> None:
            type(self).calls += 1
            raise RuntimeError("boom")

    service = BadTicker(settings, bus=memory_bus)
    task = asyncio.create_task(service._tick_forever())
    await asyncio.sleep(0.2)
    service.stop()
    await asyncio.wait_for(task, timeout=1.0)
    assert BadTicker.calls >= 2
    assert (await service.health()).errors >= 2


def test_service_describes_itself(settings, memory_bus) -> None:
    service = RecordingService(settings, memory_bus)
    described = service.describe()
    assert described["name"] == "recorder"
    assert described["group"] == "cg.recorder"
    assert described["subscribes"] == ["detections"]


def test_consumer_id_is_process_unique(settings, memory_bus) -> None:
    service = RecordingService(settings, memory_bus)
    assert service.consumer_id.startswith("recorder-")


async def test_message_context_reports_age(settings, memory_bus) -> None:
    service = RecordingService(settings, memory_bus)
    message = BusMessage.of(Topic.DETECTIONS, a_detection())
    ctx = MessageContext(service, message)
    assert ctx.age_s >= 0.0
    assert ctx.attempt >= 0


async def test_a_service_that_dead_letters_reports_itself_degraded(settings, memory_bus) -> None:  # type: ignore[no-untyped-def]
    """Containment without visibility is a quieter kind of failure.

    The dead-letter queue worked exactly as designed and hid a real failure for an entire phase: every
    track failed to persist on a SQL parameter Postgres could not type, each one was rejected,
    dead-lettered and acked, and every service kept reporting "ok" while 23,000 messages piled up in
    dlq.tracks. A service that is dropping messages must say so.
    """
    service = RejectingService(settings, bus=memory_bus)
    healthy = await service.health()
    assert healthy.status == "ok", "nothing has failed yet"

    await memory_bus.publish(Topic.DETECTIONS, a_detection())
    await service.drain(limit=1, timeout_s=2.0)

    degraded = await service.health()
    assert degraded.status == "degraded"
    assert "dead_lettered" in degraded.checks
    assert "1 message" in degraded.checks["dead_lettered"]
