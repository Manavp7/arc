"""Structured logging, trace propagation and Prometheus metrics.

The trace id set here is the same one carried on :class:`~sio_schemas.BusMessage`, so a single
signal can be followed from frame to detection to entity to event to decision to audit row
across sixteen processes by grepping one id.
"""

from __future__ import annotations

import contextvars
import logging
import sys
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from typing import Any

import structlog
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("sio_trace_id", default=None)
_tenant_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "sio_tenant_id", default=None
)
_service: contextvars.ContextVar[str | None] = contextvars.ContextVar("sio_service", default=None)

_configured = False


def set_trace_id(trace_id: str | None) -> None:
    _trace_id.set(trace_id)


def get_trace_id() -> str | None:
    return _trace_id.get()


def set_tenant_id(tenant_id: str | None) -> None:
    _tenant_id.set(tenant_id)


def get_tenant_id() -> str | None:
    return _tenant_id.get()


def set_service(name: str) -> None:
    _service.set(name)


@contextmanager
def trace_context(trace_id: str | None = None, tenant_id: str | None = None) -> Iterator[None]:
    """Bind trace/tenant for the duration of a block, restoring the previous values after.

    Used per message in the consumer loop and per request in the API middleware.
    """
    trace_token = _trace_id.set(trace_id) if trace_id is not None else None
    tenant_token = _tenant_id.set(tenant_id) if tenant_id is not None else None
    try:
        yield
    finally:
        if trace_token is not None:
            _trace_id.reset(trace_token)
        if tenant_token is not None:
            _tenant_id.reset(tenant_token)


def _add_context(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    if (trace := _trace_id.get()) is not None:
        event_dict.setdefault("trace_id", trace)
    if (tenant := _tenant_id.get()) is not None:
        event_dict.setdefault("tenant", tenant)
    if (service := _service.get()) is not None:
        event_dict.setdefault("service", service)
    return event_dict


def configure_logging(
    level: str = "INFO", fmt: str = "console", service: str | None = None
) -> None:
    """Configure structlog for this process. The most recent call wins.

    Deliberately *not* guarded by a "already configured" early return. Modules create their
    logger at import time (``log = get_logger(__name__)``), and if that implicitly locked in a
    default configuration, every later explicit call — a service setting its name and level, a
    script asking for quiet output — would be silently ignored. That bug cost an afternoon of
    "why is my --quiet flag doing nothing", so the invariant is now: configuration is cheap,
    idempotent, and always applied.
    """
    global _configured
    if service:
        set_service(service)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _add_context,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[*shared, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, redis, neo4j) through the same handler so output is
    # uniform and greppable rather than half-structured.
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=logging.WARNING, force=True)
    # Third-party log floors follow the requested level, so `--quiet`/ERROR really is quiet.
    third_party_level = max(
        logging.WARNING, logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    )
    for noisy in ("uvicorn.error", "uvicorn.access", "neo4j", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(third_party_level)
    # Neo4j logs server notifications (deprecations, planner hints) at WARNING and includes the
    # entire query text. Useful when tuning Cypher, pure noise in normal operation.
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

    _configured = True


def describe_error(exc: BaseException) -> str:
    """Render an exception for a log line, never as an empty string.

    `str(exc)` is the obvious choice and it silently loses the most important cases. httpx timeouts, several
    asyncio errors and a bare `raise SomeError` all stringify to `""`, so `error=describe_error(exc)` logs `error=` —
    and it does so precisely when something is timing out, which is when the log is all you have.

    Measured: agent proposals were vanishing and the only clue was `agents.propose_unreachable error=`. The
    type alone would have named it immediately.
    """
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def get_logger(name: str | None = None) -> Any:
    if not _configured:
        configure_logging()
    return structlog.get_logger(name) if name else structlog.get_logger()


class Metrics:
    """Per-process Prometheus metrics with a private registry.

    A private registry (rather than the global default) keeps tests hermetic: creating two
    services in one interpreter would otherwise raise duplicate-timeseries errors.
    """

    def __init__(self, service: str) -> None:
        self.service = service
        self.registry = CollectorRegistry()
        labels = {"service": service}
        self.consumed = Counter(
            "sio_messages_consumed_total",
            "Messages consumed",
            ["service", "topic"],
            registry=self.registry,
        )
        self.produced = Counter(
            "sio_messages_produced_total",
            "Messages produced",
            ["service", "topic"],
            registry=self.registry,
        )
        self.errors = Counter(
            "sio_errors_total", "Handler errors", ["service", "kind"], registry=self.registry
        )
        self.dead_lettered = Counter(
            "sio_dead_lettered_total",
            "Messages dead-lettered",
            ["service", "topic"],
            registry=self.registry,
        )
        self.handler_seconds = Histogram(
            "sio_handler_seconds",
            "Message handling latency",
            ["service", "topic"],
            buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
            registry=self.registry,
        )
        self.pipeline_seconds = Histogram(
            "sio_pipeline_seconds",
            "Age of a message when handled — the end-to-end latency signal for KPI 'time to insight'",
            ["service", "topic"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
            registry=self.registry,
        )
        self.lag = Gauge(
            "sio_consumer_lag",
            "Pending messages per topic",
            ["service", "topic"],
            registry=self.registry,
        )
        self.up = Gauge("sio_up", "Service is running", ["service"], registry=self.registry)
        self.up.labels(**labels).set(1)
        self.inference_seconds = Histogram(
            "sio_inference_seconds",
            "Model inference latency",
            ["service", "model"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
            registry=self.registry,
        )
        self.http_seconds = Histogram(
            "sio_http_seconds",
            "HTTP request latency, by route and status",
            ["service", "route", "status"],
            # Buckets chosen for what this platform's routes actually do. A copilot answer takes seconds
            # because a local model is generating tokens; an entity list takes milliseconds. A single set of
            # buckets tuned for one would make the other unreadable, so the range spans both and the p95 per
            # route is what a reader looks at.
            buckets=(0.005, 0.025, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
            registry=self.registry,
        )
        self.http_requests = Counter(
            "sio_http_requests_total",
            "HTTP requests, by route and status",
            ["service", "route", "status"],
            registry=self.registry,
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)
