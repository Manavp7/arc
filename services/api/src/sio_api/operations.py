"""Bounded, read-only operational status for the console."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI

from sio_core.config import Settings

SERVICES = (
    "ingest",
    "perception",
    "tracking",
    "fusion",
    "worldmodel",
    "spatial",
    "events",
    "prediction",
    "simulation",
    "decision",
    "copilot",
    "agents",
    "workflow",
    "alerts",
    "missions",
    "analytics",
    "governance",
    "webhooks",
)


async def collect_status(settings: Settings, client: httpx.AsyncClient) -> dict[str, Any]:
    async def probe(name: str) -> dict[str, Any]:
        port = settings.port_for(name)
        try:
            response = await client.get(f"http://127.0.0.1:{port}/health")
            response.raise_for_status()
            health = response.json()
            if not isinstance(health, dict) or "status" not in health:
                raise ValueError("invalid health response")
            if any(
                not isinstance(health.get(key, {}), dict)
                for key in ("checks", "info", "lag", "adapters")
            ):
                raise ValueError("invalid health metadata")
            return {
                "service": name,
                "status": health["status"],
                "checks": health.get("checks", {}),
                "info": health.get("info", {}),
                "pending": sum(max(0, value) for value in health.get("lag", {}).values()),
                "errors": health.get("errors", 0),
                "consumed": health.get("consumed", 0),
                "produced": health.get("produced", 0),
                "adapters": health.get("adapters", {}),
                "action": None
                if health["status"] == "ok"
                else "Inspect the reported dependency or failed-message check before relying on this service.",
            }
        except (httpx.HTTPError, ValueError, TypeError):
            return {
                "service": name,
                "status": "unreachable",
                "checks": {},
                "info": {},
                "pending": None,
                "errors": None,
                "action": "Start this service, then check its startup log and configured dependencies.",
            }

    services = await asyncio.gather(*(probe(name) for name in SERVICES))
    by_name = {row["service"]: row for row in services}
    workflow = by_name["workflow"]
    ingest = by_name["ingest"]
    worldmodel = by_name["worldmodel"]
    copilot = by_name["copilot"]
    dry_run = _reported_bool(workflow["info"].get("dry_run"))
    embedder = _reported_string(worldmodel["info"].get("embedder"))
    semantic_search = (
        False if embedder == "hash" else True if embedder and embedder.startswith("clip-") else None
    )
    copilot_mode, copilot_model = _copilot_runtime(copilot)
    adapters = settings.adapter_summary()
    # Model selectors can fall back at runtime. Never promote requested model
    # configuration into a claim that those models are actually loaded.
    adapters.update(embedder=embedder or "unknown", llm=copilot_mode)
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "status": "ok" if all(row["status"] == "ok" for row in services) else "degraded",
        "services": services,
        "adapters": adapters,
        "modes": {
            "actions": "unknown" if dry_run is None else "dry_run" if dry_run else "armed",
            "actuators": "No physical gate or drone command adapter is installed in this build.",
            "data": "unknown"
            if ingest["status"] == "unreachable"
            else ingest["info"].get("data_mode", "inspect_sources"),
            "copilot": copilot_mode,
            "copilot_model": copilot_model,
            "embedder": embedder,
            "semantic_search": semantic_search,
            "authentication": settings.auth_mode,
        },
    }


def _reported_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _reported_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return None


def _copilot_runtime(health: dict[str, Any]) -> tuple[str, str | None]:
    """Use the provider's current health report, never the gateway's selector.

    Older copilot services expose provider/model only in checks.llm. Prefer
    structured fields when available and otherwise parse that defined format.
    """
    if health["status"] == "unreachable":
        return "unknown", None
    check = _reported_string(health["checks"].get("llm")) or ""
    if "model unreachable" in check.lower() and "keyword routing" in check.lower():
        return "keyword_routing", None
    provider = _reported_string(health["info"].get("provider"))
    model = _reported_string(health["info"].get("model"))
    if provider:
        return provider, model if model not in {"n/a", "unknown"} else None
    match = re.fullmatch(r"ok \(([^:()]+): (.+)\)", check)
    if match:
        provider, model = match.groups()
        return provider.strip(), model.strip() if model.strip() not in {"n/a", "unknown"} else None
    return "unknown", None


def install_operations_routes(app: FastAPI, settings: Settings) -> None:
    @app.get("/api/system", tags=["operations"])
    async def system_status() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
            return await collect_status(settings, client)
