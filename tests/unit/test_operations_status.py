"""Operational status must distinguish missing services from healthy empty queues."""

import httpx
import pytest
from sio_api.operations import SERVICES, collect_status

from sio_core.config import Settings


@pytest.mark.asyncio
async def test_unreachable_service_does_not_claim_zero_backlog_or_armed_actions():
    def respond(request):
        raise httpx.ConnectError("unavailable", request=request)

    settings = Settings(_env_file=None, workflow_dry_run=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await collect_status(settings, client)
    assert result["status"] == "degraded"
    assert result["modes"]["actions"] == "unknown"
    assert result["modes"]["data"] == "unknown"
    assert all(row["pending"] is None for row in result["services"])
    assert len(result["services"]) == len(SERVICES)


@pytest.mark.asyncio
async def test_status_uses_running_workflow_mode_and_reports_queue_lag():
    def respond(request):
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "info": {"dry_run": "false", "data_mode": "mixed"},
                "checks": {"bus": "ok"},
                "lag": {"events": 4, "entities": 2},
            },
        )

    settings = Settings(_env_file=None, workflow_dry_run=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await collect_status(settings, client)
    assert result["status"] == "ok"
    assert result["modes"]["actions"] == "armed"
    assert result["modes"]["data"] == "mixed"
    assert all(row["pending"] == 6 for row in result["services"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime, expected", [("hash", False), ("clip-vit-base-patch32", True), (None, None)]
)
async def test_semantic_search_uses_actual_worldmodel_embedder(runtime, expected):
    settings = Settings(_env_file=None, embedder="clip")

    def respond(request):
        info = (
            {"embedder": runtime}
            if request.url.port == settings.worldmodel_port and runtime
            else {}
        )
        return httpx.Response(200, json={"status": "ok", "info": info})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await collect_status(settings, client)
    assert result["modes"]["semantic_search"] is expected
    assert result["adapters"]["embedder"] == (runtime or "unknown")


@pytest.mark.asyncio
async def test_missing_runtime_modes_do_not_fall_back_to_gateway_settings():
    settings = Settings(
        _env_file=None, embedder="clip", llm_provider="ollama", workflow_dry_run=False
    )

    def respond(request):
        return httpx.Response(200, json={"status": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await collect_status(settings, client)
    assert result["modes"]["actions"] == "unknown"
    assert result["modes"]["copilot"] == "unknown"
    assert result["modes"]["copilot_model"] is None
    assert result["modes"]["semantic_search"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "info, check, provider, model",
    [
        ({}, "ok (scripted: n/a)", "scripted", None),
        ({}, "ok (ollama: qwen3:4b)", "ollama", "qwen3:4b"),
        (
            {},
            "degraded: model unreachable, answers will use keyword routing",
            "keyword_routing",
            None,
        ),
        (
            {"provider": "openai_compat", "model": "configured-live-model"},
            "ok",
            "openai_compat",
            "configured-live-model",
        ),
    ],
)
async def test_copilot_mode_uses_reported_provider_and_fallback(info, check, provider, model):
    settings = Settings(_env_file=None, llm_provider="ollama")

    def respond(request):
        health = {"status": "ok", "checks": {}, "info": {}}
        if request.url.port == settings.copilot_port:
            health.update(info=info, checks={"llm": check})
        return httpx.Response(200, json=health)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await collect_status(settings, client)
    assert result["modes"]["copilot"] == provider
    assert result["modes"]["copilot_model"] == model
    assert result["adapters"]["llm"] == provider


@pytest.mark.asyncio
async def test_unreachable_models_report_unknown_even_when_configured():
    settings = Settings(_env_file=None, embedder="clip", llm_provider="ollama")

    def respond(request):
        if request.url.port in {settings.worldmodel_port, settings.copilot_port}:
            raise httpx.ConnectError("unavailable", request=request)
        return httpx.Response(200, json={"status": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await collect_status(settings, client)
    assert result["modes"]["semantic_search"] is None
    assert result["modes"]["copilot"] == "unknown"
    assert result["modes"]["copilot_model"] is None
