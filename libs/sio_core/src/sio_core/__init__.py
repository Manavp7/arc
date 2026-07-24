"""SIO core runtime: configuration, ports, adapters, service base, telemetry, explanations."""

from __future__ import annotations

from .config import Settings, get_settings, reset_settings
from .errors import (
    AdapterUnavailable,
    BusError,
    ConfigError,
    DependencyMissing,
    ModelUnavailable,
    NotFound,
    PolicyDenied,
    SioError,
    StoreError,
    ValidationFailed,
)
from .explain import ExplanationBuilder, merge_explanations
from .ports import BlobStore, Bus, GraphStore, VectorStore
from .registry import close_all, get_blob, get_bus, get_graph, get_pg_pool, get_vectors, override
from .service import MessageContext, SioService
from .stores.pg import PgPool
from .telemetry import (
    Metrics,
    configure_logging,
    get_logger,
    get_trace_id,
    set_trace_id,
    trace_context,
)
from .tenancy import current_tenant, tenant_scope

__version__ = "0.1.0"

__all__ = [
    "AdapterUnavailable",
    "BlobStore",
    "Bus",
    "BusError",
    "ConfigError",
    "DependencyMissing",
    "ExplanationBuilder",
    "GraphStore",
    "MessageContext",
    "Metrics",
    "ModelUnavailable",
    "NotFound",
    "PgPool",
    "PolicyDenied",
    "Settings",
    "SioError",
    "SioService",
    "StoreError",
    "ValidationFailed",
    "VectorStore",
    "__version__",
    "close_all",
    "configure_logging",
    "current_tenant",
    "get_blob",
    "get_bus",
    "get_graph",
    "get_logger",
    "get_pg_pool",
    "get_settings",
    "get_trace_id",
    "get_vectors",
    "merge_explanations",
    "override",
    "reset_settings",
    "set_trace_id",
    "tenant_scope",
    "trace_context",
]
