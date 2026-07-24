#!/usr/bin/env python3
"""Run the SIO process set without Docker, mprocs or systemd.

    uv run python scripts/supervisor.py --profile full     # everything
    uv run python scripts/supervisor.py --profile core     # ingest + api + web
    uv run python scripts/supervisor.py --profile lite     # every consumer in one process
    uv run python scripts/supervisor.py --list             # show the process table
    uv run python scripts/supervisor.py --stop             # stop a detached run

``just dev`` prefers mprocs when it is installed (the PRD's choice, and a nicer TUI), but the
platform must be runnable on a machine that does not have it — including CI, where there is no
terminal to attach to. So this supervisor exists and is the thing the e2e tests drive.

What it does beyond "start some processes":

* waits for each service's ``/health`` before starting the next tier, so a consumer never comes
  up before the bus it consumes from;
* prefixes and tees output, so one scrolling log is greppable by service;
* restarts a crashed service with exponential backoff, and gives up loudly after a few tries
  rather than flapping forever;
* stops everything in reverse order on Ctrl-C, and never kills by name — only by recorded pid.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "libs" / "sio_core" / "src"))
sys.path.insert(0, str(REPO_ROOT / "libs" / "sio_schemas" / "src"))

STATE_DIR = Path(os.environ.get("SIO_DATA_DIR", REPO_ROOT / ".sio"))
LOG_DIR = STATE_DIR / "logs"
RUN_DIR = STATE_DIR / "run"
SUPERVISOR_STATE = RUN_DIR / "supervisor.json"

COLOURS = [
    "\033[36m", "\033[32m", "\033[33m", "\033[35m", "\033[34m", "\033[91m",
    "\033[92m", "\033[93m", "\033[95m", "\033[94m", "\033[96m",
]
RESET = "\033[0m"


@dataclass
class ProcessSpec:
    """One supervised process."""

    name: str
    command: list[str]
    tier: int = 1
    """Startup tier. Everything in tier N is healthy before tier N+1 starts."""
    health_port: int | None = None
    cwd: Path = REPO_ROOT
    env: dict[str, str] = field(default_factory=dict)
    optional: bool = False
    """Optional processes log a warning and are skipped when their entry point is missing —
    which is how this file stays useful while services are still being built."""

    @property
    def module(self) -> str | None:
        if "-m" in self.command:
            return self.command[self.command.index("-m") + 1]
        return None


def python_service(name: str, port: int, tier: int, *, optional: bool = True) -> ProcessSpec:
    return ProcessSpec(
        name=name,
        command=[sys.executable, "-m", f"sio_{name}"],
        tier=tier,
        health_port=port,
        optional=optional,
    )


def build_process_table(profile: str, ports: dict[str, int], web_port: int) -> list[ProcessSpec]:
    """The process set for a profile.

    Tiers encode real dependencies: the API and world model come up before the producers, so a
    freshly started consumer group never misses the first messages, and the web UI comes last so
    its first request finds a live API.
    """
    api = ProcessSpec(
        name="api",
        command=[sys.executable, "-m", "sio_api"],
        tier=1,
        health_port=ports.get("api", 8000),
        optional=True,
    )
    web = ProcessSpec(
        name="web",
        command=["npm", "run", "dev", "--", "--port", str(web_port), "--strictPort"],
        tier=4,
        cwd=REPO_ROOT / "web",
        optional=True,
    )

    world_tier = [
        python_service("worldmodel", ports.get("worldmodel", 8105), 1),
        python_service("spatial", ports.get("spatial", 8106), 1),
        python_service("governance", ports.get("governance", 8118), 1),
    ]
    pipeline_tier = [
        python_service("perception", ports.get("perception", 8102), 2),
        python_service("tracking", ports.get("tracking", 8103), 2),
        python_service("fusion", ports.get("fusion", 8104), 2),
        python_service("events", ports.get("events", 8107), 2),
    ]
    reasoning_tier = [
        python_service("prediction", ports.get("prediction", 8108), 3),
        python_service("simulation", ports.get("simulation", 8109), 3),
        python_service("decision", ports.get("decision", 8110), 3),
        python_service("copilot", ports.get("copilot", 8111), 3),
        python_service("mcp", ports.get("mcp", 8112), 3),
        python_service("agents", ports.get("agents", 8113), 3),
        python_service("workflow", ports.get("workflow", 8114), 3),
        python_service("alerts", ports.get("alerts", 8115), 3),
        python_service("missions", ports.get("missions", 8116), 3),
        python_service("analytics", ports.get("analytics", 8117), 3),
    ]
    ingest = python_service("ingest", ports.get("ingest", 8101), 3)

    if profile == "core":
        return [api, *world_tier[:1], ingest, web]
    if profile == "lite":
        # One process hosting every consumer: ~300 MB instead of ~5 GB.
        return [
            api,
            ProcessSpec(
                name="allinone",
                command=[sys.executable, "-m", "sio_core.allinone"],
                tier=2,
                optional=True,
            ),
            ingest,
            web,
        ]
    if profile == "e2e":
        # No web server: the scenario tests drive the API directly.
        return [api, *world_tier, *pipeline_tier, *reasoning_tier, ingest]
    return [api, *world_tier, *pipeline_tier, *reasoning_tier, ingest, web]


class Supervisor:
    def __init__(self, specs: list[ProcessSpec], *, restart_limit: int = 3) -> None:
        self.specs = specs
        self.restart_limit = restart_limit
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.restarts: dict[str, int] = {}
        self.colours = {
            spec.name: COLOURS[index % len(COLOURS)] for index, spec in enumerate(specs)
        }
        self.stopping = asyncio.Event()
        self.started_at = time.monotonic()

    # ------------------------------------------------------------------- utilities
    def say(self, name: str, message: str) -> None:
        colour = self.colours.get(name, "")
        prefix = f"{colour}{name:<12}{RESET}" if colour else f"{name:<12}"
        print(f"{prefix} | {message}", flush=True)

    def entry_point_exists(self, spec: ProcessSpec) -> bool:
        """Is this process actually runnable yet?

        Services arrive over several phases; a process table that refuses to start because a
        Phase 4 service does not exist would make the repo unusable in Phase 1.
        """
        if spec.name == "web":
            return (REPO_ROOT / "web" / "package.json").exists() and (
                REPO_ROOT / "web" / "node_modules"
            ).exists()
        module = spec.module
        if module is None:
            return True
        import importlib.util

        try:
            return importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            return False

    async def wait_for_health(self, spec: ProcessSpec, timeout: float = 30.0) -> bool:
        if spec.health_port is None:
            return True
        import httpx

        deadline = time.monotonic() + timeout
        url = f"http://127.0.0.1:{spec.health_port}/health"
        async with httpx.AsyncClient(timeout=2.0) as client:
            while time.monotonic() < deadline:
                if self.stopping.is_set():
                    return False
                process = self.processes.get(spec.name)
                if process is not None and process.returncode is not None:
                    return False
                with contextlib.suppress(Exception):
                    response = await client.get(url)
                    if response.status_code == 200:
                        payload = response.json()
                        self.say(
                            spec.name,
                            f"healthy on :{spec.health_port} "
                            f"(status={payload.get('status', '?')})",
                        )
                        return True
                await asyncio.sleep(0.4)
        self.say(spec.name, f"did not become healthy on :{spec.health_port} within {timeout:.0f}s")
        return False

    # ---------------------------------------------------------------------- running
    async def start(self, spec: ProcessSpec) -> bool:
        if not self.entry_point_exists(spec):
            if spec.optional:
                self.say(spec.name, "not built yet — skipping")
                return True
            self.say(spec.name, "entry point missing")
            return False

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / f"{spec.name}.log"
        env = {**os.environ, **spec.env, "PYTHONUNBUFFERED": "1"}
        process = await asyncio.create_subprocess_exec(
            *spec.command,
            cwd=str(spec.cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self.processes[spec.name] = process
        self.say(spec.name, f"started (pid {process.pid})")
        asyncio.create_task(self._pump(spec, process, log_path))
        return True

    async def _pump(
        self, spec: ProcessSpec, process: asyncio.subprocess.Process, log_path: Path
    ) -> None:
        """Forward output to the console and to the service's own log file."""
        assert process.stdout is not None
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n--- started {time.strftime('%Y-%m-%dT%H:%M:%S')} ---\n")
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip()
                handle.write(text + "\n")
                handle.flush()
                self.say(spec.name, text)
        code = await process.wait()
        if self.stopping.is_set():
            return
        self.say(spec.name, f"exited with code {code}")
        await self._maybe_restart(spec, code)

    async def _maybe_restart(self, spec: ProcessSpec, code: int) -> None:
        if self.stopping.is_set() or code == 0:
            return
        count = self.restarts.get(spec.name, 0)
        if count >= self.restart_limit:
            self.say(
                spec.name,
                f"not restarting: {count} failures already. "
                f"see .sio/logs/{spec.name}.log",
            )
            return
        self.restarts[spec.name] = count + 1
        backoff = min(30.0, 2.0**count)
        self.say(spec.name, f"restarting in {backoff:.0f}s (attempt {count + 1})")
        await asyncio.sleep(backoff)
        if not self.stopping.is_set():
            await self.start(spec)

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stopping.set)

        tiers = sorted({spec.tier for spec in self.specs})
        for tier in tiers:
            if self.stopping.is_set():
                break
            batch = [spec for spec in self.specs if spec.tier == tier]
            print(
                f"\n=== tier {tier}: {', '.join(spec.name for spec in batch)} ===", flush=True
            )
            for spec in batch:
                await self.start(spec)
            # Only wait on processes that actually launched. Health-waiting a service that was
            # skipped as "not built yet" would add 30 seconds per phase-pending service.
            launched = [spec for spec in batch if spec.name in self.processes]
            results = await asyncio.gather(*(self.wait_for_health(spec) for spec in launched))
            unhealthy = [
                spec.name
                for spec, healthy in zip(launched, results, strict=True)
                if not healthy
            ]
            if unhealthy:
                print(f"!!! unhealthy in tier {tier}: {', '.join(unhealthy)}", flush=True)

        self._write_state()
        running = [name for name, p in self.processes.items() if p.returncode is None]
        if not running:
            print(
                "\nnothing to run: none of this profile's services are built yet.\n"
                "  see the roadmap in README.md, or try:  just doctor\n",
                flush=True,
            )
            return 0
        print(
            f"\n{len(running)} processes running: {', '.join(running)}\n"
            f"logs: .sio/logs/  ·  stop: just stop (or Ctrl-C)\n",
            flush=True,
        )

        await self.stopping.wait()
        await self.shutdown()
        return 0

    async def shutdown(self) -> None:
        print("\nstopping...", flush=True)
        self.stopping.set()
        # Reverse tier order: producers first, then consumers, then the API, so nothing is
        # publishing into a bus whose consumers have already gone.
        for spec in sorted(self.specs, key=lambda s: -s.tier):
            process = self.processes.get(spec.name)
            if process is None or process.returncode is not None:
                continue
            self.say(spec.name, "terminating")
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
        for spec in self.specs:
            process = self.processes.get(spec.name)
            if process is None:
                continue
            try:
                await asyncio.wait_for(process.wait(), timeout=8.0)
            except TimeoutError:
                self.say(spec.name, "ignored SIGTERM; killing")
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
        SUPERVISOR_STATE.unlink(missing_ok=True)
        print("stopped.", flush=True)

    def _write_state(self) -> None:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        SUPERVISOR_STATE.write_text(
            json.dumps(
                {
                    "supervisor_pid": os.getpid(),
                    "started_at": time.time(),
                    "processes": {
                        name: process.pid
                        for name, process in self.processes.items()
                        if process.returncode is None
                    },
                },
                indent=2,
            )
        )


def stop_detached() -> int:
    """Stop a previous run using the recorded pids. Never kills by name."""
    if not SUPERVISOR_STATE.exists():
        print("no supervisor state found; nothing to stop")
        return 0
    state = json.loads(SUPERVISOR_STATE.read_text())
    stopped = 0
    for name, pid in state.get("processes", {}).items():
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"  stopped {name} (pid {pid})")
            stopped += 1
        except ProcessLookupError:
            print(f"  {name} (pid {pid}) already gone")
        except PermissionError:
            print(f"  {name} (pid {pid}): permission denied")
    supervisor_pid = state.get("supervisor_pid")
    if supervisor_pid and supervisor_pid != os.getpid():
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(supervisor_pid, signal.SIGTERM)
    SUPERVISOR_STATE.unlink(missing_ok=True)
    print(f"stopped {stopped} processes")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SIO process set")
    parser.add_argument(
        "--profile", default="full", choices=["full", "core", "lite", "e2e"], help="process set"
    )
    parser.add_argument("--list", action="store_true", help="print the process table and exit")
    parser.add_argument("--stop", action="store_true", help="stop a detached run")
    parser.add_argument("--only", default="", help="comma-separated subset of service names")
    args = parser.parse_args(argv)

    if args.stop:
        return stop_detached()

    from sio_core.config import get_settings

    cfg = get_settings()
    cfg.ensure_dirs()
    ports = {
        name: cfg.port_for(name)
        for name in (
            "api", "ingest", "perception", "tracking", "fusion", "worldmodel", "spatial",
            "events", "prediction", "simulation", "decision", "copilot", "mcp", "agents",
            "workflow", "alerts", "missions", "analytics", "governance",
        )
    }
    specs = build_process_table(args.profile, ports, cfg.web_port)
    if args.only:
        wanted = {name.strip() for name in args.only.split(",") if name.strip()}
        specs = [spec for spec in specs if spec.name in wanted]

    if args.list:
        print(f"profile: {args.profile}")
        for spec in sorted(specs, key=lambda s: (s.tier, s.name)):
            port = f":{spec.health_port}" if spec.health_port else "-"
            print(f"  tier {spec.tier}  {spec.name:<12} {port:<7} {' '.join(spec.command)}")
        return 0

    supervisor = Supervisor(specs)
    try:
        return asyncio.run(supervisor.run())
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
