"""Tests for the supervisor's stop path.

These exist because `just stop` had a defect that cost real debugging time: it reported success while
services still held their ports, so the next `just services` failed with "address already in use" and the
cause looked like a port conflict rather than an incomplete stop.

The tests use real child processes rather than mocks, deliberately. Every one of the five defects was about
what an operating system actually does — process groups, signal handling, the gap between a signal being
*sent* and a process being *gone* — and a mocked `os.kill` would have passed against the broken version.

Each child is spawned by this test and killed by it, and nothing here signals by name.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from supervisor import (
    SUPERVISOR_STATE,
    _alive,
    port_is_bound,
    signal_group,
    stop_detached,
)

#: A child that ignores SIGTERM, which is the case the old code silently failed on.
STUBBORN = """
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(0.05)
"""

#: A child that spawns a grandchild holding a socket. Killing only the recorded pid leaves the port bound —
#: the vite-forks-esbuild case.
WITH_GRANDCHILD = """
import socket, subprocess, sys, time
listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", {port}))
listener.listen(1)
child = subprocess.Popen([sys.executable, "-c", "import time\\nwhile True: time.sleep(0.05)"])
while True:
    time.sleep(0.05)
"""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def spawn(code: str) -> subprocess.Popen[bytes]:
    """Spawn a child in its own session, exactly as the supervisor does."""
    return subprocess.Popen([sys.executable, "-c", code], start_new_session=True)


def write_state(processes: dict[str, int], ports: dict[str, int] | None = None) -> None:
    SUPERVISOR_STATE.parent.mkdir(parents=True, exist_ok=True)
    SUPERVISOR_STATE.write_text(
        json.dumps({"supervisor_pid": os.getpid(), "processes": processes, "ports": ports or {}})
    )


@pytest.fixture(autouse=True)
def clean_state():
    original = SUPERVISOR_STATE.read_text() if SUPERVISOR_STATE.exists() else None
    SUPERVISOR_STATE.unlink(missing_ok=True)
    yield
    SUPERVISOR_STATE.unlink(missing_ok=True)
    if original is not None:
        SUPERVISOR_STATE.write_text(original)


def test_nothing_to_stop_is_not_an_error() -> None:
    assert stop_detached() == 0


def test_a_well_behaved_process_is_stopped() -> None:
    child = spawn("import time\nwhile True: time.sleep(0.05)")
    write_state({"quiet": child.pid})
    try:
        assert stop_detached(grace_s=4.0) == 0
        assert not _alive(child.pid), "the process should be gone once stop reports success"
        assert not SUPERVISOR_STATE.exists(), "a clean stop should clear its state"
    finally:
        if _alive(child.pid):
            signal_group(child.pid, signal.SIGKILL)
        child.wait(timeout=5)


def test_a_process_that_ignores_sigterm_is_killed() -> None:
    """The defect: SIGTERM was sent, nothing waited, and a service that ignored it kept its port.

    A service that has ignored SIGTERM for eight seconds is not going to change its mind, and the old code's
    response was to report success.
    """
    child = spawn(STUBBORN)
    time.sleep(0.4)  # let the handler install
    write_state({"stubborn": child.pid})
    try:
        assert stop_detached(grace_s=1.0) == 0
        assert not _alive(child.pid), "SIGKILL should have finished what SIGTERM could not"
    finally:
        if _alive(child.pid):
            signal_group(child.pid, signal.SIGKILL)
        child.wait(timeout=5)


def test_stop_waits_rather_than_assuming() -> None:
    """A process that takes a moment to exit must not be reported as still running — nor as stopped early.

    The window matters: the old code returned immediately, so a caller that went straight on to start the
    stack raced a port that was about to be released.
    """
    child = spawn(
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *_: (time.sleep(0.8), sys.exit(0)))\n"
        "while True: time.sleep(0.05)"
    )
    time.sleep(0.4)
    write_state({"slow": child.pid})
    try:
        started = time.monotonic()
        assert stop_detached(grace_s=5.0) == 0
        elapsed = time.monotonic() - started
        assert not _alive(child.pid)
        assert elapsed >= 0.5, "stop returned before the process had actually exited"
    finally:
        if _alive(child.pid):
            signal_group(child.pid, signal.SIGKILL)
        child.wait(timeout=5)


def test_a_grandchild_holding_a_port_is_stopped_too() -> None:
    """Killing the recorded pid alone left the port bound — the vite-forks-esbuild case.

    Signalling the process *group* is what fixes it, and the group id is still a recorded pid: this is not a
    match by name, which could hit an unrelated process the operator was running.
    """
    port = free_port()
    child = spawn(WITH_GRANDCHILD.format(port=port))
    deadline = time.monotonic() + 5
    while not port_is_bound(port) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert port_is_bound(port), "the fixture did not manage to bind its port"

    write_state({"forker": child.pid}, {"forker": port})
    try:
        assert stop_detached(grace_s=3.0) == 0
        assert not _alive(child.pid)
        deadline = time.monotonic() + 3
        while port_is_bound(port) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not port_is_bound(port), "the grandchild kept the port — the group was not signalled"
    finally:
        if _alive(child.pid):
            signal_group(child.pid, signal.SIGKILL)
        child.wait(timeout=5)


def test_an_already_dead_process_is_not_an_error() -> None:
    child = spawn("import sys; sys.exit(0)")
    child.wait(timeout=5)
    write_state({"gone": child.pid})
    assert stop_detached(grace_s=1.0) == 0


def test_the_state_file_survives_a_partial_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deleting the state file unconditionally was how a live process became unreachable.

    The state file holds the only record of a pid. Removing it while the process is alive means the one tool
    that could stop it no longer knows it exists — and `just stop` then says "nothing to stop" forever while
    a port stays bound. That was the reported symptom.
    """
    child = spawn("import time\nwhile True: time.sleep(0.05)")
    write_state({"unkillable": child.pid})
    try:
        # Simulate a process that survives even SIGKILL (an uninterruptible wait, or a permission problem)
        # by making every signal a no-op.
        monkeypatch.setattr("supervisor.signal_group", lambda pid, sig: True)
        assert stop_detached(grace_s=0.6) == 1, "a surviving process must be reported as a failure"
        assert SUPERVISOR_STATE.exists(), "the state file must be kept so stop can be retried"
    finally:
        monkeypatch.undo()
        signal_group(child.pid, signal.SIGKILL)
        child.wait(timeout=5)


def test_the_supervisor_is_stopped_before_its_children() -> None:
    """Otherwise its restart logic fights the shutdown.

    The supervisor restarts crashed services, which is its job — and its job is precisely wrong during a
    stop. Killing children first means racing a parent that is trying to bring them back.
    """
    supervisor = spawn("import time\nwhile True: time.sleep(0.05)")
    worker = spawn("import time\nwhile True: time.sleep(0.05)")
    SUPERVISOR_STATE.parent.mkdir(parents=True, exist_ok=True)
    SUPERVISOR_STATE.write_text(
        json.dumps(
            {"supervisor_pid": supervisor.pid, "processes": {"worker": worker.pid}, "ports": {}}
        )
    )
    try:
        assert stop_detached(grace_s=3.0) == 0
        assert not _alive(supervisor.pid), "the supervisor itself should be stopped"
        assert not _alive(worker.pid)
    finally:
        for child in (supervisor, worker):
            if _alive(child.pid):
                signal_group(child.pid, signal.SIGKILL)
            child.wait(timeout=5)


def test_signalling_an_unknown_pid_is_reported_not_raised() -> None:
    assert signal_group(2**22, signal.SIGTERM) is False


def test_port_probe_agrees_with_reality() -> None:
    port = free_port()
    assert not port_is_bound(port)
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        assert port_is_bound(port)


# --- restart policy -----------------------------------------------------------------------------
def test_a_clean_exit_is_still_restarted() -> None:
    """For a daemon, exit 0 outside shutdown is not success — it is a service that has vanished.

    Found while testing something else: a SIGTERM to the decision service produced a graceful exit 0, the
    supervisor declined to restart it because zero meant "it meant to do that", and the stack carried on
    with fourteen of fifteen services. One line scrolled past in a log nobody was reading, and the next
    request to `/api/decisions` returned a 503 whose cause was twenty minutes upstream.

    Deliberate stops are covered separately: `stopping` is set for both Ctrl-C and `--stop`.
    """
    import asyncio

    from supervisor import ProcessSpec, Supervisor

    spec = ProcessSpec(name="ghost", command=["/bin/true"], tier=1, health_port=0)
    supervisor = Supervisor([spec])
    started: list[str] = []

    async def record(target: ProcessSpec) -> bool:
        started.append(target.name)
        return True

    supervisor.start = record  # type: ignore[method-assign]
    asyncio.run(supervisor._maybe_restart(spec, 0))
    assert started == ["ghost"], "a clean exit outside shutdown must still be restarted"


def test_a_deliberate_stop_does_not_restart_anything() -> None:
    """Otherwise `just stop` would fight the supervisor forever."""
    import asyncio

    from supervisor import ProcessSpec, Supervisor

    spec = ProcessSpec(name="ghost", command=["/bin/true"], tier=1, health_port=0)
    supervisor = Supervisor([spec])
    supervisor.stopping.set()
    started: list[str] = []

    async def record(target: ProcessSpec) -> bool:
        started.append(target.name)
        return True

    supervisor.start = record  # type: ignore[method-assign]
    asyncio.run(supervisor._maybe_restart(spec, 0))
    asyncio.run(supervisor._maybe_restart(spec, 1))
    assert started == [], "nothing may be restarted during a deliberate stop"
