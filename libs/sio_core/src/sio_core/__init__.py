"""SIO core runtime: configuration, ports, adapters, service base, telemetry, explanations."""

from __future__ import annotations

from .authn import ANONYMOUS, DevJwtAuth, Principal, ServiceIdentity, build_authenticator
from .authz import Decision, authorise, policy_engine, require
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
from .guard import action_for, install_governance, principal_of
from .ports import BlobStore, Bus, GraphStore, VectorStore
from .registry import (
    close_all,
    get_blob,
    get_bus,
    get_embedder,
    get_graph,
    get_llm,
    get_pg_pool,
    get_vectors,
    override,
)
from .service import MessageContext, SioService
from .stores.pg import PgPool
from .telemetry import (
    Metrics,
    configure_logging,
    describe_error,
    get_logger,
    get_trace_id,
    set_trace_id,
    trace_context,
)
from .tenancy import current_tenant, tenant_scope

__version__ = "0.1.0"

__all__ = [
    "ANONYMOUS",
    "AdapterUnavailable",
    "BlobStore",
    "Bus",
    "BusError",
    "ConfigError",
    "Decision",
    "DependencyMissing",
    "DevJwtAuth",
    "ExplanationBuilder",
    "GraphStore",
    "MessageContext",
    "Metrics",
    "ModelUnavailable",
    "NotFound",
    "PgPool",
    "PolicyDenied",
    "Principal",
    "ServiceIdentity",
    "Settings",
    "SioError",
    "SioService",
    "StoreError",
    "ValidationFailed",
    "VectorStore",
    "__version__",
    "action_for",
    "authorise",
    "build_authenticator",
    "close_all",
    "configure_logging",
    "current_tenant",
    "describe_error",
    "get_blob",
    "get_bus",
    "get_embedder",
    "get_graph",
    "get_llm",
    "get_logger",
    "get_pg_pool",
    "get_settings",
    "get_trace_id",
    "get_vectors",
    "install_governance",
    "merge_explanations",
    "override",
    "policy_engine",
    "principal_of",
    "require",
    "reset_settings",
    "set_trace_id",
    "tenant_scope",
    "trace_context",
]
