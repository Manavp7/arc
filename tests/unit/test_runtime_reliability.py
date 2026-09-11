"""Regression checks for complete lite startup and truthful workflow activity outcomes."""

from __future__ import annotations

import asyncio
import importlib.util
import socket
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from sio_workflow.activities import ActivityContext, generate_report
from sio_workflow.playbooks import Playbook, StepSpec
from sio_workflow.runner import InlineRunner
from sio_workflow.service import WorkflowService

from sio_core.allinone import CONSUMERS, ConsumerHost
from sio_core.config import Settings
from sio_core.service import SioService

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_lite_includes_every_consumer_and_http_mcp():
    root = REPO_ROOT
    spec = importlib.util.spec_from_file_location(
        "supervisor_contract", root / "scripts/supervisor.py"
    )
    import sys

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    full = module.build_process_table("full", {}, 5173)
    lite = module.build_process_table("lite", {"allinone": 18999}, 5173)
    assert {name for name, _ in CONSUMERS} == {p.name for p in full} - {
        "api",
        "ingest",
        "web",
        "mcp",
    }
    aggregate = next(p for p in lite if p.name == "allinone")
    assert aggregate.health_port == 18999
    assert not aggregate.optional
    assert "--http" in next(p for p in lite if p.name == "mcp").command


async def test_partial_lite_setup_releases_resources(tmp_path, monkeypatch):
    closed = AsyncMock()
    monkeypatch.setattr("sio_core.allinone.registry.close_all", closed)
    order = []

    class Working(SioService):
        name = "working"

        async def setup(self):
            order.append("setup")

        async def teardown(self):
            order.append("teardown")

    class Failing(Working):
        name = "failing"

        async def setup(self):
            raise RuntimeError("missing database")

    cfg = Settings(_env_file=None, data_dir=tmp_path, bus_backend="memory")
    host = ConsumerHost([Working(cfg), Failing(cfg)], cfg)
    with pytest.raises(RuntimeError, match="missing database"):
        await host.run(install_signals=False)
    assert order == ["setup", "teardown", "teardown"]
    assert not host.ready
    closed.assert_awaited_once()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_lite_retains_service_routes_and_closes_listeners(tmp_path, monkeypatch):
    closed = AsyncMock()
    monkeypatch.setattr("sio_core.allinone.registry.close_all", closed)
    torn_down = []
    service_port = free_port()
    host_port = free_port()
    cfg = Settings(_env_file=None, data_dir=tmp_path, bus_backend="memory", allinone_port=host_port)

    class Probe(SioService):
        name = "probe"

        @property
        def port(self):
            return service_port

        def routes(self, app):
            @app.get("/health/probe")
            async def probe():
                return {"retained": True}

        async def teardown(self):
            torn_down.append(self.name)

    service = Probe(cfg)
    host = ConsumerHost([service], cfg)
    task = asyncio.create_task(host.run(install_signals=False))
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            for _ in range(100):
                if task.done():
                    await task
                if host.ready:
                    break
                await asyncio.sleep(0.02)
            response = await client.get(f"http://127.0.0.1:{host_port}/health")
            assert response.status_code == 200
            assert response.json()["checks"] == {"probe": "ok"}
            response = await client.get(f"http://127.0.0.1:{service_port}/health")
            assert response.json()["service"] == "probe"
    finally:
        host.stop()
        await asyncio.wait_for(task, timeout=8)
    assert torn_down == ["probe"]
    closed.assert_awaited_once()
    with socket.socket() as sock:
        assert sock.connect_ex(("127.0.0.1", service_port)) != 0


def context(client, **kwargs):
    return ActivityContext(
        api_url="http://api",
        ingest_url="http://ingest",
        tenant_id="acme",
        run_id="report-run",
        zone_id="dock_3",
        trigger_event_id="evt-trigger",
        bearer_token="service-token",
        client=client,
        **kwargs,
    )


async def test_report_authenticates_and_keeps_unrelated_events_out():
    def respond(request):
        assert request.headers["authorization"] == "Bearer service-token"
        return httpx.Response(
            200,
            json=[
                {"tenant_id": "acme", "event_id": "evt-trigger", "zone_id": "dock_3"},
                {"tenant_id": "acme", "event_id": "other", "zone_id": "other"},
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await generate_report(context(client), "report")
    assert result["report"]["events_considered"] == 2
    assert result["report"]["events_included"] == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"detail": "unauthenticated"}),
        httpx.Response(503, json={"detail": "unavailable"}),
        httpx.Response(200, json={"unexpected": []}),
        httpx.Response(200, json=[{"tenant_id": "other", "event_id": "secret"}]),
    ],
)
async def test_failed_report_is_failed_optional_step(response):
    playbook = Playbook(
        name="Report",
        description="test",
        steps=(
            StepSpec(step_id="report", name="report", activity="generate_report", optional=True),
        ),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
        outcome = await InlineRunner().execute(playbook, context(client))
    assert outcome.ok  # Optional reporting must not undo completed response steps.
    step = outcome.run.steps[0]
    assert step.status == "failed"
    assert step.error
    assert step.output["report"] is None


async def test_armed_actuator_step_fails_instead_of_claiming_completion():
    playbook = Playbook(
        name="Gate",
        description="test",
        steps=(StepSpec(step_id="gate", name="gate", activity="close_gate"),),
    )
    outcome = await InlineRunner().execute(playbook, context(None, dry_run=False))
    assert not outcome.ok
    assert outcome.run.steps[0].output["closed"] is False
    assert "no gate actuator" in outcome.run.steps[0].error


def test_temporal_selection_is_rejected_before_startup(tmp_path):
    cfg = Settings(_env_file=None, data_dir=tmp_path, workflow_runner="temporal")
    with pytest.raises(ValueError, match="not implemented"):
        WorkflowService(cfg)


def test_workflow_cooldowns_and_recent_history_are_tenant_scoped():
    from sio_workflow.playbooks import FIRE_RESPONSE
    from sio_workflow.runner import RunLedger

    from sio_schemas import WorkflowRun

    ledger = RunLedger()
    assert ledger.may_start(FIRE_RESPONSE, "dock_3", tenant_id="alpha")
    assert not ledger.may_start(FIRE_RESPONSE, "dock_3", tenant_id="alpha")
    assert ledger.may_start(FIRE_RESPONSE, "dock_3", tenant_id="beta")
    ledger.record(WorkflowRun(run_id="alpha-run", tenant_id="alpha", playbook="Fire"))
    ledger.record(WorkflowRun(run_id="beta-run", tenant_id="beta", playbook="Fire"))
    assert ledger.get("alpha-run", tenant_id="beta") is None
    alpha = ledger.describe(tenant_id="alpha")
    beta = ledger.describe(tenant_id="beta")
    assert [run["run_id"] for run in alpha["recent"]] == ["alpha-run"]
    assert [run["run_id"] for run in beta["recent"]] == ["beta-run"]
    assert alpha["suppressed_by_cooldown"] == 1
    assert beta["suppressed_by_cooldown"] == 0


async def test_registry_closes_sync_and_async_adapters_once(monkeypatch):
    from sio_core import registry

    closed = []

    class Synchronous:
        def close(self):
            closed.append("sync")

    class Asynchronous:
        async def close(self):
            closed.append("async")

    monkeypatch.setattr(registry, "_instances", {"sync": Synchronous(), "async": Asynchronous()})
    await registry.close_all()
    await registry.close_all()
    assert closed == ["sync", "async"]
    assert not registry._instances


async def test_consumer_host_health_names_failed_dependencies(tmp_path):
    class Unconfigured(SioService):
        name = "spatial"

        async def health_checks(self):
            return {"zones": "no zones (run: just seed)"}

    cfg = Settings(_env_file=None, data_dir=tmp_path, bus_backend="memory")
    host = ConsumerHost([Unconfigured(cfg)], cfg)
    host.ready = True
    response = await host.health()
    import json

    payload = json.loads(response.body)
    assert response.status_code == 503
    assert payload["failures"]["spatial"]["zones"] == "no zones (run: just seed)"


async def test_setup_seed_preserves_existing_site_configuration(tmp_path, monkeypatch):
    root = REPO_ROOT
    spec = importlib.util.spec_from_file_location("seed_contract", root / "scripts/seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pool = AsyncMock()
    pool.ping.return_value = True
    pool.fetchval.return_value = 1
    monkeypatch.setattr("sio_core.stores.pg.PgPool", lambda *args, **kwargs: pool)
    cfg = Settings(_env_file=None, data_dir=tmp_path)
    monkeypatch.setattr("sio_core.config.get_settings", lambda: cfg)
    assert await module.seed(clear=False, write_geojson=False, if_empty=True) == 0
    pool.execute.assert_not_awaited()
    pool.close.assert_awaited_once()


async def test_forecast_fitting_leaves_consumer_event_loop_responsive(tmp_path, monkeypatch):
    import threading

    from sio_prediction.service import PredictionService
    from sio_prediction.targets import SPECS

    main_thread = threading.get_ident()
    entered = threading.Event()
    release = threading.Event()

    def fit(*args, **kwargs):
        assert threading.get_ident() != main_thread
        entered.set()
        assert release.wait(timeout=3)
        return "forecast-result"

    monkeypatch.setattr("sio_prediction.service.build", fit)
    service = PredictionService(Settings(_env_file=None, data_dir=tmp_path))
    task = asyncio.create_task(service._build_target(SPECS["throughput"], None))
    try:
        for _ in range(50):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        assert not task.done()  # This coroutine kept running while the model was occupied.
    finally:
        release.set()
    assert await task == "forecast-result"


@pytest.mark.parametrize("operation", ["recommend", "schedule", "backtest"])
async def test_cpu_heavy_http_requests_allow_other_requests(tmp_path, monkeypatch, operation):
    """A busy solver/model must not prevent the shared host from serving another request."""
    import threading
    from datetime import timedelta
    from types import SimpleNamespace

    from fastapi import FastAPI
    from sio_decision.service import DecisionService
    from sio_decision.solvers import Incident
    from sio_prediction.service import PredictionService

    from sio_schemas import utc_now

    entered = threading.Event()
    release = threading.Event()
    main_thread = threading.get_ident()

    def compute(*args, **kwargs):
        assert threading.get_ident() != main_thread
        entered.set()
        assert release.wait(timeout=3)
        if operation == "recommend":
            return [], {}
        if operation == "schedule":
            return SimpleNamespace(describe=lambda: {"status": "EMPTY"})
        return None

    cfg = Settings(_env_file=None, data_dir=tmp_path, decision_use_llm=False)
    if operation == "backtest":
        service = PredictionService(cfg)
        now = utc_now()
        service.pool = AsyncMock()
        service.pool.fetch.return_value = [
            {"source_id": "sensor", "ts": now - timedelta(minutes=index), "value": 20.0}
            for index in range(1, 30)
        ]
        monkeypatch.setattr("sio_prediction.service.backtest", compute)
        method, path = "GET", "/predict/backtest"
    else:
        service = DecisionService(cfg)
        service.pool = AsyncMock()
        if operation == "recommend":
            service._incident_from = AsyncMock(
                return_value=Incident(incident_id="incident", kind="fire", lat=0, lon=0)
            )
            service._responders = AsyncMock(return_value=[])
            service._persist = AsyncMock()
            service._emit = AsyncMock()
            monkeypatch.setattr("sio_decision.service.build_options", compute)
            path = "/decisions/recommend"
        else:
            service.pool.fetch.side_effect = [[], [{"zone_id": "dock_3"}]]
            monkeypatch.setattr("sio_decision.solvers.solve_dock_schedule", compute)
            path = "/decisions/schedule/docks"
        method = "POST"

    app = FastAPI()
    service.routes(app)

    @app.get("/probe")
    async def probe():
        return {"responsive": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://host"
    ) as client:
        task = asyncio.create_task(client.request(method, path))
        try:
            for _ in range(50):
                if entered.is_set() or task.done():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            assert not task.done()
            response = await asyncio.wait_for(client.get("/probe"), timeout=0.5)
            assert response.json() == {"responsive": True}
        finally:
            release.set()
            completed = await task
        assert completed.status_code == 200
