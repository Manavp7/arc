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
import subprocess
import sys
import time
from collections.abc import Sequence
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
    "\033[36m",
    "\033[32m",
    "\033[33m",
    "\033[35m",
    "\033[34m",
    "\033[91m",
    "\033[92m",
    "\033[93m",
    "\033[95m",
    "\033[94m",
    "\033[96m",
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


def service_exists(name: str) -> bool:
    """Whether a service's package is actually present.

    The process table lists services from the whole roadmap, including ones a later phase will add. Launching
    a module that does not exist wastes the supervisor's three restart attempts on a `ModuleNotFoundError`,
    reports the tier as unhealthy, and buries the real startup output under three tracebacks — every single
    boot.

    Checked against the filesystem rather than by import, because importing a service pulls in its whole
    dependency graph and this runs before anything is up.
    """
    return (REPO_ROOT / "services" / name / "src" / f"sio_{name}").is_dir()


def python_service(
    name: str, port: int, tier: int, *, optional: bool = True, args: Sequence[str] = ()
) -> ProcessSpec:
    return ProcessSpec(
        name=name,
        command=[sys.executable, "-m", f"sio_{name}", *args],
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
        # --http, because `python -m sio_mcp` alone speaks stdio: it would sit waiting on a pipe nobody
        # is writing to, with no port for the supervisor to health-check. Desktop clients launch the
        # stdio form themselves; a supervised service is the HTTP one.
        python_service("mcp", ports.get("mcp", 8112), 3, args=("--http",)),
        python_service("agents", ports.get("agents", 8113), 3),
        python_service("workflow", ports.get("workflow", 8114), 3),
        python_service("alerts", ports.get("alerts", 8115), 3),
        python_service("missions", ports.get("missions", 8116), 3),
        python_service("analytics", ports.get("analytics", 8117), 3),
        python_service("webhooks", ports.get("webhooks", 8119), 3),
        # Tier 1, not 3: it persists the audit trail, and records produced before it is up are buffered on
        # the bus but not written. Starting it early narrows the window in which a denial goes unrecorded.
    ]
    ingest = python_service("ingest", ports.get("ingest", 8101), 3)

    # Drop services whose package does not exist yet.
    #
    # `missions` sits in this table for a phase that has not been built, and every boot spent three restart
    # attempts on it before giving up — which also made the tier report unhealthy and pushed the real startup
    # output off the screen. Filtering here means the table can name the whole roadmap without the supervisor
    # pretending to run it.
    def present(specs: list[ProcessSpec]) -> list[ProcessSpec]:
        keep, absent = [], []
        for spec in specs:
            if spec.name == "web" or service_exists(spec.name):
                keep.append(spec)
            else:
                absent.append(spec.name)
        if absent:
            print(
                f"  note: not yet built, so not started: {', '.join(sorted(absent))}",
                flush=True,
            )
        return keep

    world_tier = present(world_tier)
    pipeline_tier = present(pipeline_tier)
    reasoning_tier = present(reasoning_tier)

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
        # Output-forwarding tasks are held here: an un-referenced task may be
        # garbage-collected mid-run, which would silently stop tailing a service.
        self._pumps: set[asyncio.Task[None]] = set()
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

    async def wait_for_health(self, spec: ProcessSpec, timeout_s: float = 30.0) -> bool:
        if spec.health_port is None:
            return True
        import httpx

        deadline = time.monotonic() + timeout_s
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
                            f"healthy on :{spec.health_port} (status={payload.get('status', '?')})",
                        )
                        return True
                await asyncio.sleep(0.4)
        self.say(
            spec.name, f"did not become healthy on :{spec.health_port} within {timeout_s:.0f}s"
        )
        return False

    # ---------------------------------------------------------------------- running
    @staticmethod
    def port_in_use(port: int) -> bool:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            return probe.connect_ex(("127.0.0.1", port)) == 0

    async def start(self, spec: ProcessSpec) -> bool:
        if not self.entry_point_exists(spec):
            if spec.optional:
                self.say(spec.name, "not built yet — skipping")
                return True
            self.say(spec.name, "entry point missing")
            return False

        # A port already in use means a previous run is still alive. Uvicorn's own failure for this
        # is a twenty-line traceback that buries the actual cause, so say it plainly and skip.
        if spec.health_port and self.port_in_use(spec.health_port):
            self.say(
                spec.name,
                f"port {spec.health_port} is already in use — a previous run is still alive. "
                f"stop it with: just stop",
            )
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
            # Its own session, so the service becomes a process-group leader and the entire group can be
            # signalled by its recorded pgid. Without this, a service that spawns children — vite forks
            # esbuild, uvicorn's reloader forks a worker — leaves grandchildren holding the port after the
            # recorded pid is killed, which is one of the ways `just stop` used to "succeed" while the
            # ports stayed busy.
            #
            # It also detaches the group from the supervisor's terminal, so a Ctrl-C in the shell reaches
            # the supervisor alone and shutdown stays ordered rather than every service receiving SIGINT
            # simultaneously.
            start_new_session=True,
        )
        self.processes[spec.name] = process
        self.say(spec.name, f"started (pid {process.pid})")
        # Rewritten on every spawn, not once at startup. A service restarted after a crash gets a NEW pid,
        # and a state file written only at boot cannot see it — so `--stop` would miss exactly the
        # processes most likely to be misbehaving.
        self._write_state()
        pump = asyncio.create_task(self._pump(spec, process, log_path), name=f"pump-{spec.name}")
        self._pumps.add(pump)
        pump.add_done_callback(self._pumps.discard)
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
        """Restart a service that exited when it was not asked to — including a clean exit.

        `code == 0` used to mean "it meant to do that" and skip the restart. For a *daemon* that is wrong:
        every process here is a long-running server, none of them is supposed to exit at all, and a clean
        exit outside shutdown is not a success — it is a service that has vanished.

        Found while testing: SIGTERM to the decision service produced a graceful exit 0, the supervisor
        declined to restart it, and the stack carried on with fourteen of fifteen services. One line
        scrolled past in a log nobody was reading. The next request to `/api/decisions` returned a 503 whose
        cause was twenty minutes upstream.

        Deliberate stops are already covered: `self.stopping` is set for both Ctrl-C and `--stop`, and this
        returns immediately in that case. So anything reaching the restart logic is, by definition,
        unexpected.
        """
        if self.stopping.is_set():
            return
        if code == 0:
            self.say(
                spec.name,
                "exited cleanly, which a long-running service should never do — restarting anyway",
            )
        count = self.restarts.get(spec.name, 0)
        if count >= self.restart_limit:
            self.say(
                spec.name,
                f"not restarting: {count} failures already. see .sio/logs/{spec.name}.log",
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
            print(f"\n=== tier {tier}: {', '.join(spec.name for spec in batch)} ===", flush=True)
            for spec in batch:
                await self.start(spec)
            # Only wait on processes that actually launched. Health-waiting a service that was
            # skipped as "not built yet" would add 30 seconds per phase-pending service.
            launched = [spec for spec in batch if spec.name in self.processes]
            results = await asyncio.gather(*(self.wait_for_health(spec) for spec in launched))
            unhealthy = [
                spec.name for spec, healthy in zip(launched, results, strict=True) if not healthy
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
            signal_group(process.pid, signal.SIGTERM)
        for spec in self.specs:
            process = self.processes.get(spec.name)
            if process is None:
                continue
            try:
                await asyncio.wait_for(process.wait(), timeout=8.0)
            except TimeoutError:
                self.say(spec.name, "ignored SIGTERM; killing")
                signal_group(process.pid, signal.SIGKILL)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=3.0)

        # The only success criterion that means anything: are the ports free? Counting signals sent
        # measures effort, not effect, and a supervisor that says "stopped." while eight ports are still
        # bound has told a comfortable lie that the next `just services` will expose as a confusing
        # "address already in use".
        held = still_held_ports(self.specs)
        if held:
            print(
                "warning: these ports are still in use after shutdown: "
                + ", ".join(f"{name}:{port}" for name, port in held),
                flush=True,
            )
            print("the state file has been kept so `just stop` can try again.", flush=True)
            return
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
                    # Recorded rather than re-derived, so `--stop` verifies against the ports this run
                    # actually bound. Reading them from settings at stop time would silently check the
                    # wrong ones if the config changed while the stack was up.
                    "ports": {
                        spec.name: spec.health_port for spec in self.specs if spec.health_port
                    },
                },
                indent=2,
            )
        )


def signal_group(pid: int, sig: int) -> bool:
    """Signal a process's whole group, falling back to the process alone.

    Group-wide because a service's children hold ports too: killing the recorded pid of a `vite` that has
    forked `esbuild` leaves the web port bound. Services are spawned with `start_new_session=True`, so the
    pid IS the group id and this remains a signal to recorded pids only — never a match by name, which
    could hit an unrelated process an operator was running.
    """
    try:
        os.killpg(os.getpgid(pid), sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    except OSError:
        # No such group, or the process is not a group leader after all. Fall back to the pid itself
        # rather than giving up: a partial stop is much better than none.
        try:
            os.kill(pid, sig)
            return True
        except OSError:
            return False


def port_is_bound(port: int) -> bool:
    """Whether anything is listening on a port right now."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def still_held_ports(specs: Sequence[ProcessSpec]) -> list[tuple[str, int]]:
    return [
        (spec.name, spec.health_port)
        for spec in specs
        if spec.health_port and port_is_bound(spec.health_port)
    ]


def _is_zombie(pid: int) -> bool:
    """Whether a pid is a zombie — exited, but not yet reaped by its parent.

    This is not a detail. `os.kill(pid, 0)` succeeds for a zombie, because the pid still occupies a slot in
    the process table until somebody reaps it. A stop path built on that check concludes the process
    survived SIGKILL, which is alarming, wrong, and was the first thing these tests caught: a perfectly
    well-behaved child was reported as "STILL running after SIGKILL".

    A zombie runs no code and holds no ports. For the purpose of "is this still running", it is gone.

    Both branches matter, because macOS is the primary target and has no `/proc`. Linux gets the cheap path
    (a file read); everything else shells out to `ps`, which BSD and Linux both spell the same way here.
    """
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():
        try:
            # The comm field is parenthesised and may itself contain spaces and brackets, so the state
            # character is the first field after the LAST closing paren — not simply the third token.
            raw = proc_stat.read_text()
            return raw[raw.rindex(")") + 1 :].strip()[:1] == "Z"
        except (OSError, ValueError):
            return False
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "state=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.stdout.strip().startswith("Z")


def _alive(pid: int) -> bool:
    """Whether a pid is running — which a zombie is not."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Signalling was refused, so it exists and belongs to somebody else. Treated as alive, which is the
        # safe direction: reporting a foreign process as stopped would be a lie.
        return True
    return not _is_zombie(pid)


def stop_detached(*, grace_s: float = 8.0) -> int:
    """Stop a previous run using the recorded pids — and confirm it actually happened.

    The old version sent SIGTERM and returned. That produced the defect this rewrite exists to fix:
    `just stop` reporting success while services still held their ports, so the next `just services`
    failed with "address already in use" and the cause looked like a port conflict rather than an
    incomplete stop.

    Four things are now true that were not:

    * signals go to the process **group**, so a service's children go too;
    * the function **waits** for each process to actually exit, rather than assuming SIGTERM worked;
    * anything still alive after the grace period gets SIGKILL, because a service that ignores SIGTERM is
      not going to change its mind;
    * the state file is **kept** when something survives, so a second `just stop` can finish the job.
      Deleting it unconditionally was how a live process became unreachable by the only tool that knew its
      pid.
    """
    if not SUPERVISOR_STATE.exists():
        print("no supervisor state found; nothing to stop")
        return 0

    state = json.loads(SUPERVISOR_STATE.read_text())
    recorded: dict[str, int] = {
        name: int(pid) for name, pid in (state.get("processes") or {}).items()
    }
    supervisor_pid = state.get("supervisor_pid")

    # The supervisor first, so it does not restart the very children being stopped. Its restart logic is
    # doing its job — and its job is precisely wrong during a shutdown.
    if supervisor_pid and int(supervisor_pid) != os.getpid() and _alive(int(supervisor_pid)):
        print(
            f"  stopping supervisor (pid {supervisor_pid}) first, so it stops restarting children"
        )
        signal_group(int(supervisor_pid), signal.SIGTERM)
        deadline = time.monotonic() + grace_s
        while _alive(int(supervisor_pid)) and time.monotonic() < deadline:
            time.sleep(0.2)
        if _alive(int(supervisor_pid)):
            signal_group(int(supervisor_pid), signal.SIGKILL)

    signalled: list[tuple[str, int]] = []
    for name, pid in recorded.items():
        if not _alive(pid):
            print(f"  {name} (pid {pid}) already gone")
            continue
        if signal_group(pid, signal.SIGTERM):
            print(f"  SIGTERM → {name} (pid {pid})")
            signalled.append((name, pid))
        else:
            print(f"  {name} (pid {pid}): could not signal")

    # Wait, rather than hope.
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not any(_alive(pid) for _, pid in signalled):
            break
        time.sleep(0.25)

    stubborn = [(name, pid) for name, pid in signalled if _alive(pid)]
    for name, pid in stubborn:
        print(f"  {name} (pid {pid}) ignored SIGTERM after {grace_s:.0f}s; SIGKILL")
        signal_group(pid, signal.SIGKILL)
    if stubborn:
        time.sleep(0.5)

    survivors = [(name, pid) for name, pid in signalled if _alive(pid)]
    stopped = len(signalled) - len(survivors)
    print(f"stopped {stopped} process group(s)")

    if survivors:
        for name, pid in survivors:
            print(f"  warning: {name} (pid {pid}) is STILL running after SIGKILL")
        print("keeping the state file so `just stop` can be run again")
        return 1

    # Ports checked last, because a pid can exit while its socket lingers in TIME_WAIT — and because a
    # port held by something the supervisor never started is worth naming rather than silently blaming the
    # next startup for.
    ports: dict[str, int] = {name: int(port) for name, port in (state.get("ports") or {}).items()}
    lingering = [
        (name, port) for name, port in ports.items() if name in recorded and port_is_bound(port)
    ]
    if lingering:
        print(
            "  note: still bound (a socket in TIME_WAIT, or a process this supervisor did not start): "
            + ", ".join(f"{name}:{port}" for name, port in lingering)
        )

    SUPERVISOR_STATE.unlink(missing_ok=True)
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
            "api",
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
            "mcp",
            "agents",
            "workflow",
            "alerts",
            "missions",
            "analytics",
            "governance",
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
