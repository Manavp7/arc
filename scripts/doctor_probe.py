#!/usr/bin/env python3
"""Deep environment checks for ``just doctor``.

An open port proves a process is listening, nothing more. These checks use the platform's own
adapters to answer the questions that actually matter: are the credentials right, are the
extensions installed, is the schema applied, is the bucket writable, is the graph reachable.

Output is a simple line protocol consumed by ``scripts/doctor.sh``::

    ok <message>
    warn <message>
    fail <message>|<remedy>
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "libs" / "sio_core" / "src"))
sys.path.insert(0, str(REPO_ROOT / "libs" / "sio_schemas" / "src"))

EXPECTED_TABLES = {
    "tenants",
    "sources",
    "observations",
    "detections",
    "tracks",
    "frames",
    "embeddings",
    "entities",
    "entity_states",
    "relationships",
    "zones",
    "events",
    "forecasts",
    "decisions",
    "alerts",
    "missions",
    "workflow_runs",
    "simulation_runs",
    "webhooks",
    "measurements",
    "audit_log",
    "schema_migrations",
}

failures = 0


def emit(status: str, message: str, remedy: str = "") -> None:
    global failures
    if status == "fail":
        failures += 1
        print(f"fail {message}|{remedy}" if remedy else f"fail {message}")
    else:
        print(f"{status} {message}")


async def check_postgres() -> None:
    from sio_core.config import get_settings
    from sio_core.stores.pg import PgPool

    cfg = get_settings()
    pool = PgPool(cfg.pg_dsn, min_size=1, max_size=2)
    try:
        if not await pool.ping():
            emit("fail", f"postgres unreachable at {cfg.pg_host}:{cfg.pg_port}", "just services")
            return
        emit("ok", f"postgres reachable as {cfg.pg_user}@{cfg.pg_database}")

        extensions = await pool.extensions()
        for required in ("postgis", "vector"):
            if required in extensions:
                emit("ok", f"extension {required} installed")
            else:
                emit(
                    "fail",
                    f"extension {required} missing",
                    "just db-init (install postgis/pgvector first)",
                )

        rows = await pool.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
        present = {row["table_name"] for row in rows}
        missing = EXPECTED_TABLES - present
        if missing:
            emit("fail", f"schema incomplete, missing {len(missing)} tables", "just db-init")
        else:
            emit("ok", f"schema applied ({len(present)} tables)")

        # An append-only table that accepts UPDATE is a governance failure, not a warning.
        triggers = await pool.fetch(
            "SELECT tgname FROM pg_trigger WHERE tgname LIKE '%append_only%' AND NOT tgisinternal"
        )
        if len(triggers) >= 4:
            emit("ok", f"append-only enforcement active ({len(triggers)} triggers)")
        else:
            emit("fail", "append-only triggers missing", "just db-init")
    finally:
        await pool.close()


async def check_redis() -> None:
    from sio_core import registry
    from sio_core.config import get_settings

    cfg = get_settings()
    if cfg.bus_backend != "redis":
        emit("warn", f"bus backend is {cfg.bus_backend}, skipping redis check")
        return
    bus = registry.get_bus(cfg)
    if await bus.ping():
        emit("ok", f"redis reachable at {cfg.redis_url}")
        topics = ["raw.frames", "detections", "tracks", "entities", "events"]
        lengths = []
        for topic in topics:
            length = await bus.length(topic)  # type: ignore[attr-defined]
            if length:
                lengths.append(f"{topic}={length}")
        emit(
            "ok",
            f"streams: {', '.join(lengths)}" if lengths else "streams: empty (nothing seeded yet)",
        )
    else:
        emit("fail", f"redis unreachable at {cfg.redis_url}", "just services")
    await bus.close()


async def check_graph() -> None:
    from sio_core import registry
    from sio_core.config import get_settings

    cfg = get_settings()
    graph = registry.get_graph(cfg)
    try:
        if not await graph.ping():
            if cfg.graph_backend == "neo4j":
                emit(
                    "fail",
                    "neo4j unreachable or credentials rejected",
                    "just services && just neo4j-init",
                )
            else:
                emit("fail", f"graph backend {cfg.graph_backend} unreachable", "just services")
            return
        counts = await graph.counts(tenant_id=cfg.tenant_id)
        emit(
            "ok",
            f"graph ({cfg.graph_backend}) reachable: "
            f"{counts.get('entities', 0)} entities, {counts.get('relationships', 0)} edges",
        )
        if cfg.graph_backend == "neo4j":
            constraints = await graph.raw_query("SHOW CONSTRAINTS", tenant_id=cfg.tenant_id)
            indexes = await graph.raw_query("SHOW INDEXES", tenant_id=cfg.tenant_id)
            if constraints:
                emit(
                    "ok",
                    f"neo4j schema applied ({len(constraints)} constraints, {len(indexes)} indexes)",
                )
            else:
                emit("fail", "neo4j constraints not applied", "just neo4j-init")
    finally:
        await graph.close()


async def check_blob() -> None:
    from sio_core import registry
    from sio_core.config import get_settings

    cfg = get_settings()
    blob = registry.get_blob(cfg)
    try:
        if not await blob.ping():
            emit(
                "fail",
                f"blob store ({cfg.blob_backend}) unreachable or bucket missing",
                "just services && just minio-init",
            )
            return
        # Prove writability: a bucket that exists but rejects writes fails at the first frame.
        probe = ".sio-doctor-probe"
        await blob.put(probe, b"ok", content_type="text/plain")
        readback = await blob.get(probe)
        await blob.delete(probe)
        if readback == b"ok":
            emit("ok", f"blob store ({cfg.blob_backend}) readable and writable")
        else:
            emit("fail", "blob store round-trip mismatch", "check minio credentials in .env")
    except Exception as exc:
        emit("fail", f"blob store error: {exc}", "just services && just minio-init")
    finally:
        await blob.close()


async def check_vectors() -> None:
    from sio_core import registry
    from sio_core.config import get_settings
    from sio_core.stores.vectors import DEFAULT_DIM

    cfg = get_settings()
    vectors = registry.get_vectors(cfg)
    try:
        if not await vectors.ping():
            emit("fail", f"vector store ({cfg.vector_backend}) unreachable", "just services")
            return
        probe = [0.0] * DEFAULT_DIM
        probe[0] = 1.0
        await vectors.upsert(
            "doctor", "probe", probe, tenant_id=cfg.tenant_id, metadata={"probe": True}
        )
        hits = await vectors.search("doctor", probe, tenant_id=cfg.tenant_id, limit=1)
        await vectors.delete("doctor", "probe", tenant_id=cfg.tenant_id)
        if hits and hits[0][1] > 0.99:
            emit("ok", f"vector store ({cfg.vector_backend}) search working, {DEFAULT_DIM}-d")
        else:
            emit("fail", "vector similarity search returned nothing", "just db-init")
    except Exception as exc:
        emit("fail", f"vector store error: {exc}", "just db-init")
    finally:
        await vectors.close()


async def check_llm() -> None:
    import httpx

    from sio_core.config import get_settings

    cfg = get_settings()
    if cfg.llm_provider == "scripted":
        emit("warn", "LLM provider is 'scripted' (deterministic, no model needed)")
        return
    if cfg.llm_provider == "ollama":
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{cfg.ollama_url}/api/tags")
                models = [m["name"] for m in response.json().get("models", [])]
        except Exception:
            emit("warn", f"ollama not reachable at {cfg.ollama_url} — run: just models")
            return
        if cfg.llm_model in models:
            emit("ok", f"ollama has the pinned model {cfg.llm_model}")
        else:
            emit(
                "warn",
                f"ollama is up but {cfg.llm_model} is not pulled "
                f"({len(models)} other models) — run: just models",
            )


async def main() -> int:
    from sio_core.config import get_settings
    from sio_core.telemetry import configure_logging

    # The probe's own output *is* the report; adapter construction logs would bury it.
    configure_logging(level="ERROR", fmt="console", service="doctor")
    cfg = get_settings()
    print(f"effective adapters: {cfg.adapter_summary()}")
    for check in (check_postgres, check_redis, check_graph, check_vectors, check_blob, check_llm):
        try:
            await check()
        except Exception as exc:
            emit("fail", f"{check.__name__} raised {type(exc).__name__}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
