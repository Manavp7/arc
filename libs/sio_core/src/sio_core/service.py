"""``SioService``: the runtime every SIO service inherits.

A service becomes a class with a name, a list of topics and an ``on_message`` handler. This
base supplies everything the PRD's non-functional requirements ask for and that no service
should re-implement:

- structured logging with the message's ``trace_id`` bound for the duration of the handler;
- ``/health`` (with dependency checks, adapter choices and consumer lag) and ``/metrics``;
- at-least-once consumption with an idempotency cache, so redelivery is harmless;
- retry with dead-lettering, so one poison message cannot wedge a stream;
- a periodic ``tick()`` hook for work that is not message-driven;
- graceful shutdown on SIGINT/SIGTERM that drains in-flight work and closes adapters.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

import uvicorn
from fastapi import FastAPI, Response

from sio_schemas import BusMessage, HealthStatus, SioModel
from sio_schemas.base import SCHEMA_VERSION

from . import registry
from .config import Settings, get_settings
from .errors import SioError
from .ports import Bus
from .telemetry import Metrics, configure_logging, get_logger, trace_context


class MessageContext:
    """Per-message handle passed to :meth:`SioService.on_message`."""

    __slots__ = ("attempt", "message", "service", "topic")

    def __init__(self, service: SioService, message: BusMessage) -> None:
        self.service = service
        self.message = message
        self.topic = message.topic
        self.attempt = message.delivery_count

    async def publish(self, topic: str, model: SioModel) -> str:
        """Publish downstream, inheriting this message's trace id.

        Using this instead of ``bus.publish`` is what keeps a single signal's evidence chain
        intact across the whole pipeline.
        """
        return await self.service.publish(topic, model, trace_id=self.message.trace_id)

    @property
    def age_s(self) -> float:
        """Seconds since the message was published — the end-to-end latency signal."""
        from sio_schemas import utc_now

        return (utc_now() - self.message.ts).total_seconds()


class SioService:
    """Base class for every SIO service.

    Subclasses set :attr:`name` and (optionally) :attr:`subscribes`, then implement
    :meth:`on_message`. Services with no subscriptions (the API, the MCP server) still get
    config, logging, health and metrics.
    """

    name: str = "service"
    subscribes: Sequence[str] = ()
    tick_interval_s: float | None = None
    idempotency_cache_size: int = 20_000

    def __init__(self, settings: Settings | None = None, *, bus: Bus | None = None) -> None:
        self.settings = settings or get_settings()
        configure_logging(self.settings.log_level, self.settings.log_format, service=self.name)
        self.log = get_logger(f"sio.{self.name}")
        self.metrics = Metrics(self.name)
        self._bus = bus
        self._started_at = time.monotonic()
        self._stopping = asyncio.Event()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._counters = {"consumed": 0, "produced": 0, "errors": 0, "dead_lettered": 0}
        self._extra_checks: dict[str, str] = {}
        self._app: FastAPI | None = None

    @property
    def app(self) -> FastAPI:
        """The HTTP application, built on first access.

        Deliberately lazy. Building it in ``__init__`` would call the subclass's ``routes()`` hook
        before the subclass had finished its own ``__init__`` — so a route closure referencing
        ``self.read`` would raise ``AttributeError`` at import time. Lazy construction means a
        subclass can set up whatever its routes need, in the normal order, after calling
        ``super().__init__()``.
        """
        if self._app is None:
            self._app = self._build_app()
        return self._app

    # ------------------------------------------------------------------- plumbing
    @property
    def bus(self) -> Bus:
        if self._bus is None:
            self._bus = registry.get_bus(self.settings)
        return self._bus

    @property
    def group(self) -> str:
        from .bus.codec import group_name

        return group_name(self.name)

    @property
    def consumer_id(self) -> str:
        """Unique per process, so two replicas of a service share work rather than duplicate it."""
        return f"{self.name}-{os.getpid()}"

    @property
    def port(self) -> int:
        return self.settings.port_for(self.name)

    async def publish(self, topic: str, model: SioModel, *, trace_id: str | None = None) -> str:
        stream_id = await self.bus.publish(str(topic), model, producer=self.name, trace_id=trace_id)
        self._counters["produced"] += 1
        self.metrics.produced.labels(service=self.name, topic=str(topic)).inc()
        return stream_id

    # ----------------------------------------------------------------- overridables
    async def setup(self) -> None:
        """Acquire resources. Raise to abort startup."""

    async def teardown(self) -> None:
        """Release resources. Must not raise."""

    async def on_message(self, message: BusMessage, ctx: MessageContext) -> None:
        """Handle one message. Raising triggers redelivery (then dead-lettering)."""
        raise NotImplementedError

    async def tick(self) -> None:
        """Periodic work, every :attr:`tick_interval_s` seconds."""

    async def health_checks(self) -> dict[str, str]:
        """Extra dependency checks merged into ``/health``.

        Return values starting with ``ok`` are healthy — ``"ok (18 agents)"`` is fine. Anything
        else marks the service degraded, so put counters and other non-status detail in
        :meth:`health_info` instead.
        """
        return {}

    async def health_info(self) -> dict[str, str]:
        """Informational values for ``/health`` that must not affect status."""
        return {}

    def routes(self, app: FastAPI) -> None:
        """Hook for services that expose their own HTTP API."""

    # ------------------------------------------------------------------- HTTP app
    def _build_app(self) -> FastAPI:
        app = FastAPI(
            title=f"SIO {self.name}",
            version="0.1.0",
            docs_url="/docs",
            openapi_url="/openapi.json",
        )

        @app.get("/health", response_model=HealthStatus, tags=["ops"])
        async def health() -> HealthStatus:
            return await self.health()

        @app.get("/metrics", tags=["ops"])
        async def metrics() -> Response:
            if not self.settings.metrics_enabled:
                return Response(status_code=404)
            return Response(content=self.metrics.render(), media_type="text/plain; version=0.0.4")

        self.routes(app)
        return app

    async def health(self) -> HealthStatus:
        checks: dict[str, str] = {}
        status = "ok"
        try:
            checks["bus"] = "ok" if await self.bus.ping() else "unreachable"
        except Exception as exc:
            checks["bus"] = f"error: {exc}"
        try:
            checks.update(await self.health_checks())
        except Exception as exc:
            checks["custom"] = f"error: {exc}"
        # Dead-lettered messages degrade health. This is not decoration.
        #
        # The dead-letter queue is containment: a bad message is set aside so it cannot wedge a stream.
        # It worked exactly as designed and thereby hid a real failure for an entire phase — every
        # single track failed to persist (a SQL parameter Postgres could not type), each one was
        # rejected, dead-lettered and acked, and the pipeline carried on looking healthy. 23,000
        # messages in `dlq.tracks` and a service reporting "ok".
        #
        # Containment without visibility is just a quieter kind of failure, so a service that is
        # dropping messages now says so.
        rejected = int(self._counters.get("dead_lettered", 0))
        if rejected:
            checks["dead_lettered"] = (
                f"degraded: {rejected} message(s) rejected and dead-lettered — see dlq.* streams"
            )

        checks.update(self._extra_checks)
        # A check is healthy when it *starts with* "ok", so a service can report useful detail
        # ("ok (18 agents, 81 frames)") without declaring itself broken.
        if any(not str(value).lower().startswith("ok") for value in checks.values()):
            status = "degraded"

        info: dict[str, str] = {}
        try:
            info = await self.health_info()
        except Exception as exc:
            info = {"health_info_error": str(exc)}

        lag: dict[str, int] = {}
        for topic in self.subscribes:
            with contextlib.suppress(Exception):
                value = await self.bus.lag(str(topic), self.group)
                lag[str(topic)] = value
                self.metrics.lag.labels(service=self.name, topic=str(topic)).set(value)

        return HealthStatus(
            service=self.name,
            status=status,
            schema_version=SCHEMA_VERSION,
            uptime_s=round(time.monotonic() - self._started_at, 3),
            checks=checks,
            info=info,
            consumed=self._counters["consumed"],
            produced=self._counters["produced"],
            errors=self._counters["errors"],
            lag=lag,
            adapters=self.settings.adapter_summary(),
        )

    # -------------------------------------------------------------- consumer loop
    def _is_duplicate(self, message_id: str) -> bool:
        """Bounded LRU of handled message ids.

        At-least-once delivery means a message can arrive twice (a crash between handling and
        acking, or an ``XAUTOCLAIM`` reclaim). Handlers are written to be idempotent anyway,
        but skipping the obvious repeats saves the work and keeps counters honest.
        """
        if message_id in self._seen:
            self._seen.move_to_end(message_id)
            return True
        self._seen[message_id] = None
        if len(self._seen) > self.idempotency_cache_size:
            self._seen.popitem(last=False)
        return False

    async def _consume_forever(self) -> None:
        topics = [str(t) for t in self.subscribes]
        if not topics:
            return
        self.log.info("consumer.start", topics=topics, group=self.group)
        while not self._stopping.is_set():
            try:
                async for message in self.bus.consume(
                    topics,
                    group=self.group,
                    consumer=self.consumer_id,
                    block_ms=self.settings.bus_block_ms,
                    batch=self.settings.bus_batch,
                ):
                    if self._stopping.is_set():
                        break
                    await self._handle(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._counters["errors"] += 1
                self.metrics.errors.labels(service=self.name, kind="consume").inc()
                self.log.error("consumer.error", error=str(exc), exc_info=True)
                await asyncio.sleep(1.0)

    async def _handle(self, message: BusMessage) -> None:
        topic = str(message.topic)
        if self._is_duplicate(message.id):
            self.log.debug("message.duplicate", message_id=message.id, topic=topic)
            if message.stream_id:
                await self.bus.ack(topic, self.group, message.stream_id)
            return

        ctx = MessageContext(self, message)
        started = time.perf_counter()
        with trace_context(message.trace_id, message.tenant_id):
            try:
                self.metrics.pipeline_seconds.labels(service=self.name, topic=topic).observe(
                    max(0.0, ctx.age_s)
                )
                await self.on_message(message, ctx)
                self._counters["consumed"] += 1
                self.metrics.consumed.labels(service=self.name, topic=topic).inc()
                if message.stream_id:
                    await self.bus.ack(topic, self.group, message.stream_id)
            except SioError as exc:
                # A domain error is a bad message, not a broken service: dead-letter it rather
                # than redelivering something that will fail identically forever.
                self._counters["errors"] += 1
                self.metrics.errors.labels(service=self.name, kind="domain").inc()
                self._counters["dead_lettered"] += 1
                self.log.error(
                    "message.rejected",
                    topic=topic,
                    error=str(exc),
                    dead_lettered_total=self._counters["dead_lettered"],
                )
                await self.bus.dead_letter(message, str(exc))
                self.metrics.dead_lettered.labels(service=self.name, topic=topic).inc()
                if message.stream_id:
                    await self.bus.ack(topic, self.group, message.stream_id)
            except Exception as exc:
                # Unexpected failure: leave it unacked so it is retried, and let the bus
                # dead-letter it once the retry budget is spent.
                self._counters["errors"] += 1
                self.metrics.errors.labels(service=self.name, kind="handler").inc()
                self.log.error(
                    "message.failed",
                    topic=topic,
                    attempt=ctx.attempt,
                    error=str(exc),
                    exc_info=True,
                )
            finally:
                self.metrics.handler_seconds.labels(service=self.name, topic=topic).observe(
                    time.perf_counter() - started
                )

    async def _tick_forever(self) -> None:
        if not self.tick_interval_s:
            return
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.tick_interval_s)
                return
            except TimeoutError:
                pass
            try:
                await self.tick()
            except Exception as exc:
                self._counters["errors"] += 1
                self.metrics.errors.labels(service=self.name, kind="tick").inc()
                self.log.error("tick.failed", error=str(exc), exc_info=True)

    # -------------------------------------------------------------------- lifecycle
    async def serve(self) -> None:
        """Run the service until a shutdown signal arrives."""
        self.settings.ensure_dirs()
        self.log.info(
            "service.starting",
            port=self.port,
            subscribes=[str(t) for t in self.subscribes],
            adapters=self.settings.adapter_summary(),
        )
        await self.setup()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stopping.set)

        config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=self.port,
            log_level=self.settings.log_level.lower(),
            access_log=False,
            lifespan="on",
        )
        server = uvicorn.Server(config)
        tasks = [
            asyncio.create_task(server.serve(), name=f"{self.name}-http"),
            asyncio.create_task(self._consume_forever(), name=f"{self.name}-consume"),
            asyncio.create_task(self._tick_forever(), name=f"{self.name}-tick"),
        ]
        self.log.info("service.ready", port=self.port)

        await self._stopping.wait()
        self.log.info("service.stopping")
        server.should_exit = True
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        try:
            await self.teardown()
        except Exception as exc:
            self.log.warning("teardown.failed", error=str(exc))
        await registry.close_all()
        self.log.info("service.stopped", **self._counters)

    def run(self) -> None:
        """Entry point for ``python -m sio_<service>``."""
        with contextlib.suppress(KeyboardInterrupt):  # pragma: no cover - interactive
            asyncio.run(self.serve())

    def stop(self) -> None:
        self._stopping.set()

    # ------------------------------------------------------------------ test helper
    async def drain(self, limit: int = 1000, *, timeout_s: float = 5.0) -> int:
        """Consume up to ``limit`` messages, then return. Used by tests and one-shot jobs."""
        topics = [str(t) for t in self.subscribes]
        if not topics:
            return 0
        handled = 0
        deadline = time.monotonic() + timeout_s
        agen = self.bus.consume(
            topics, group=self.group, consumer=self.consumer_id, block_ms=100, batch=limit
        )
        try:
            while handled < limit and time.monotonic() < deadline:
                try:
                    message = await asyncio.wait_for(
                        anext(agen),
                        timeout=max(0.05, deadline - time.monotonic()),
                    )
                except (TimeoutError, StopAsyncIteration):
                    break
                await self._handle(message)
                handled += 1
        finally:
            # The port promises an AsyncIterator; only generators can be closed. Closing when
            # possible releases the adapter's consumer state promptly instead of at GC time.
            closer = getattr(agen, "aclose", None)
            if closer is not None:
                with contextlib.suppress(Exception):
                    await closer()
        return handled

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "port": self.port,
            "subscribes": [str(t) for t in self.subscribes],
            "group": self.group,
            "adapters": self.settings.adapter_summary(),
        }
