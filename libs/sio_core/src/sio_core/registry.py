"""Adapter registry — the one place that maps configuration to concrete implementations.

Services call ``get_bus()`` / ``get_graph()`` / … and never import an adapter module. That is
what makes the PRD §9.3 swap matrix real: pointing SIO at Kafka, Memgraph or Qdrant is an
environment change, and the code that consumes the port is untouched.

``tests/unit/test_architecture.py`` fails the build if a service imports an adapter directly.
"""

from __future__ import annotations

from typing import Any

from .config import Settings, get_settings
from .errors import ConfigError
from .ports import BlobStore, Bus, GraphStore, VectorStore
from .telemetry import get_logger

log = get_logger("sio.registry")

_instances: dict[str, Any] = {}


def _cached(key: str, factory: Any) -> Any:
    """Memoise adapters per process: connection pools should be shared, not re-created.

    An explicit :func:`override` for the bare port name (e.g. ``"bus"``) always wins, so a
    test can inject a fake before the first real lookup happens.
    """
    port = key.split(":", 1)[0]
    if port in _instances:
        return _instances[port]
    if key not in _instances:
        _instances[key] = factory()
    return _instances[key]


def get_pg_pool(settings: Settings | None = None) -> Any:
    cfg = settings or get_settings()
    from .stores.pg import PgPool

    return _cached(
        "pg",
        lambda: PgPool(cfg.pg_dsn, min_size=cfg.pg_pool_min, max_size=cfg.pg_pool_max),
    )


def get_bus(settings: Settings | None = None) -> Bus:
    cfg = settings or get_settings()
    backend = cfg.bus_backend

    def factory() -> Bus:
        if backend == "memory":
            from .bus.memory import MemoryBus

            log.info("registry.bus", backend="memory")
            return MemoryBus(maxlen=cfg.bus_maxlen)
        if backend == "redis":
            from .bus.redis_bus import RedisStreamBus

            log.info("registry.bus", backend="redis", url=cfg.redis_url)
            return RedisStreamBus(
                cfg.redis_url,
                maxlen=cfg.bus_maxlen,
                block_ms=cfg.bus_block_ms,
                batch=cfg.bus_batch,
                claim_idle_ms=cfg.bus_claim_idle_ms,
                max_retries=cfg.bus_max_retries,
            )
        if backend == "kafka":  # pragma: no cover - Phase 7
            raise ConfigError(
                "the Kafka bus adapter lands in Phase 7; use SIO_BUS_BACKEND=redis for now"
            )
        raise ConfigError(f"unknown SIO_BUS_BACKEND={backend!r}")

    return _cached(f"bus:{backend}", factory)


def get_graph(settings: Settings | None = None) -> GraphStore:
    cfg = settings or get_settings()
    backend = cfg.graph_backend

    def factory() -> GraphStore:
        if backend == "memory":
            from .stores.graph_memory import MemoryGraphStore

            log.info("registry.graph", backend="memory")
            return MemoryGraphStore()
        if backend == "postgres":
            from .stores.graph_pg import PostgresGraphStore

            log.info("registry.graph", backend="postgres")
            return PostgresGraphStore(get_pg_pool(cfg))
        if backend == "neo4j":
            from .stores.graph_neo4j import Neo4jGraphStore

            log.info("registry.graph", backend="neo4j", uri=cfg.neo4j_uri)
            return Neo4jGraphStore(
                cfg.neo4j_uri, cfg.neo4j_user, cfg.neo4j_password, cfg.neo4j_database
            )
        raise ConfigError(f"unknown SIO_GRAPH_BACKEND={backend!r}")

    return _cached(f"graph:{backend}", factory)


def get_vectors(settings: Settings | None = None) -> VectorStore:
    cfg = settings or get_settings()
    backend = cfg.vector_backend

    def factory() -> VectorStore:
        if backend == "memory":
            from .stores.vectors import MemoryVectorStore

            log.info("registry.vectors", backend="memory")
            return MemoryVectorStore()
        if backend == "pgvector":
            from .stores.vectors import PgVectorStore

            log.info("registry.vectors", backend="pgvector")
            return PgVectorStore(get_pg_pool(cfg))
        if backend == "qdrant":  # pragma: no cover - Phase 7
            raise ConfigError(
                "the Qdrant adapter lands in Phase 7; use SIO_VECTOR_BACKEND=pgvector for now"
            )
        raise ConfigError(f"unknown SIO_VECTOR_BACKEND={backend!r}")

    return _cached(f"vectors:{backend}", factory)


def get_blob(settings: Settings | None = None) -> BlobStore:
    cfg = settings or get_settings()
    backend = cfg.blob_backend

    def factory() -> BlobStore:
        if backend == "file":
            from .stores.blob import FileBlobStore

            root = cfg.data_dir / "blobs"
            log.info("registry.blob", backend="file", root=str(root))
            return FileBlobStore(root)
        if backend == "minio":
            from .stores.blob import MinioBlobStore

            log.info("registry.blob", backend="minio", endpoint=cfg.minio_endpoint)
            return MinioBlobStore(
                cfg.minio_endpoint,
                cfg.minio_access_key,
                cfg.minio_secret_key,
                cfg.minio_bucket,
                secure=cfg.minio_secure,
            )
        raise ConfigError(f"unknown SIO_BLOB_BACKEND={backend!r}")

    return _cached(f"blob:{backend}", factory)


def override(name: str, instance: Any) -> None:
    """Inject an adapter (tests, or a service that builds its own).

    ``name`` is one of ``bus``, ``graph``, ``vectors``, ``blob`` — the *unqualified* port name,
    which shadows any backend-specific entry.
    """
    _instances[name] = instance
    for key in [k for k in _instances if k.startswith(f"{name}:")]:
        _instances[key] = instance


async def close_all() -> None:
    """Close every constructed adapter. Called on service shutdown."""
    for key, instance in list(_instances.items()):
        close = getattr(instance, "close", None)
        if close is None:
            continue
        try:
            await close()
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            log.warning("registry.close_failed", adapter=key, error=str(exc))
    _instances.clear()


def reset() -> None:
    """Forget all instances without closing them (unit tests)."""
    _instances.clear()
