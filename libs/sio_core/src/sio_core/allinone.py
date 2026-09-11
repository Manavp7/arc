"""The low-memory consumer host used by ``just dev-lite``.

Consumers keep their ordinary HTTP ports and routes. One owner handles process signals,
startup failure, shutdown and shared adapters; calling each service's ``serve`` would race
signal handlers and close the registry while other consumers were still using it.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import signal
from collections.abc import Iterator, Sequence
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import registry
from .config import Settings, get_settings
from .service import SioService
from .telemetry import set_service

# Keep the dependency tiers in order. Ingest, API and MCP remain separate processes.
CONSUMERS = (
    ("worldmodel", "WorldModelService"),
    ("spatial", "SpatialService"),
    ("governance", "GovernanceService"),
    ("perception", "PerceptionService"),
    ("tracking", "TrackingService"),
    ("fusion", "FusionService"),
    ("events", "EventsService"),
    ("prediction", "PredictionService"),
    ("simulation", "SimulationService"),
    ("decision", "DecisionService"),
    ("copilot", "CopilotService"),
    ("agents", "AgentsService"),
    ("workflow", "WorkflowService"),
    ("alerts", "AlertsService"),
    ("missions", "MissionsService"),
    ("analytics", "AnalyticsService"),
    ("webhooks", "WebhooksService"),
)


def build_consumers(settings: Settings) -> list[SioService]:
    return [
        getattr(importlib.import_module(f"sio_{name}.service"), class_name)(settings=settings)
        for name, class_name in CONSUMERS
    ]


class SharedServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        """The host owns SIGINT/SIGTERM once for every listener."""
        yield


class ConsumerHost:
    def __init__(self, services: Sequence[SioService], settings: Settings) -> None:
        self.services = list(services)
        self.settings = settings
        self.stopping = asyncio.Event()
        self.ready = False
        self.servers: list[SharedServer] = []
        self.tasks: list[asyncio.Task[Any]] = []
        self.app = FastAPI(title="SIO consumer host")
        self.app.add_api_route("/health", self.health, methods=["GET"])

    async def health(self) -> JSONResponse:
        reports = await asyncio.gather(*(service.health() for service in self.services))
        checks = {report.service: report.status for report in reports}
        failures = {report.service: report.checks for report in reports if report.status != "ok"}
        healthy = self.ready and all(value == "ok" for value in checks.values())
        return JSONResponse(
            {
                "service": "allinone",
                "status": "ok" if healthy else "degraded",
                "checks": checks,
                "failures": failures,
            },
            status_code=200 if healthy else 503,
        )

    def stop(self) -> None:
        self.stopping.set()

    async def run(self, *, install_signals: bool = True) -> None:
        self.settings.ensure_dirs()
        loop = asyncio.get_running_loop()
        if install_signals:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self.stop)
        prepared: list[SioService] = []
        stop_task: asyncio.Task[Any] | None = None
        try:
            for service in self.services:
                # Even a partially failed setup may own resources requiring teardown.
                prepared.append(service)
                set_service(service.name)
                await service.setup()
            endpoints = [(service.name, service.app, service.port) for service in self.services]
            endpoints.append(("allinone", self.app, self.settings.allinone_port))
            for name, app, port in endpoints:
                set_service(name)
                server = SharedServer(
                    uvicorn.Config(
                        app, host="127.0.0.1", port=port, log_level="warning", access_log=False
                    )
                )
                self.servers.append(server)
                self.tasks.append(asyncio.create_task(server.serve(), name=f"http:{port}"))
            for service in self.services:
                set_service(service.name)
                if service.subscribes:
                    self.tasks.append(
                        asyncio.create_task(
                            service._consume_forever(), name=f"consume:{service.name}"
                        )
                    )
                if service.tick_interval_s:
                    self.tasks.append(
                        asyncio.create_task(service._tick_forever(), name=f"tick:{service.name}")
                    )
            set_service("allinone")
            stop_task = asyncio.create_task(self.stopping.wait(), name="host:stop")
            while not all(server.started for server in self.servers):
                if self.stopping.is_set():
                    return
                for task in self.tasks:
                    if task.done():
                        task.result()
                        raise RuntimeError(
                            f"consumer host task exited during startup: {task.get_name()}"
                        )
                await asyncio.sleep(0.02)
            self.ready = True
            done, _ = await asyncio.wait(
                [*self.tasks, stop_task], return_when=asyncio.FIRST_COMPLETED
            )
            if stop_task not in done:
                task = next(iter(done))
                task.result()
                raise RuntimeError(f"consumer host task exited unexpectedly: {task.get_name()}")
        finally:
            self.ready = False
            self.stopping.set()
            for service in self.services:
                service.stop()
            for server in self.servers:
                server.should_exit = True
            for task in self.tasks:
                if not task.get_name().startswith("http:"):
                    task.cancel()
            if self.tasks:
                _, pending = await asyncio.wait(self.tasks, timeout=5.0)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*self.tasks, return_exceptions=True)
            if stop_task is not None:
                stop_task.cancel()
                await asyncio.gather(stop_task, return_exceptions=True)
            for service in reversed(prepared):
                set_service(service.name)
                try:
                    await service.teardown()
                except Exception:
                    service.log.exception("allinone.teardown_failed")
            set_service("allinone")
            await registry.close_all()
            if install_signals:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.remove_signal_handler(sig)


def main() -> None:
    settings = get_settings()
    asyncio.run(ConsumerHost(build_consumers(settings), settings).run())


if __name__ == "__main__":
    main()
