"""Exercise service startup without binding ports or touching installed datastores."""

from __future__ import annotations

import io
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


@pytest.fixture
def service_sandbox(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("services.sh", "lib.sh", "versions.env"):
        shutil.copyfile(REPO_ROOT / "scripts" / name, scripts / name)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    # Simulate a system-owned listener invisible to the invoking user's lsof.
    _executable(binaries / "uname", 'printf "%s\\n" "${TEST_PLATFORM:-Linux}"\n')
    _executable(binaries / "lsof", 'echo lsof >> "$TEST_CALLS"; exit 1\n')
    _executable(
        binaries / "nc",
        'echo "nc $*" >> "$TEST_CALLS"\n[ "${TEST_PORT_OPEN:-1}" = 1 ] || [ -f "$TEST_STARTED" ]\n',
    )
    _executable(
        binaries / "python3",
        'echo "ping $*" >> "$TEST_CALLS"\ncat >/dev/null\nexit "${TEST_PING_STATUS:-0}"\n',
    )
    _executable(binaries / "redis-server", ': > "$TEST_STARTED"\n')
    _executable(binaries / "brew", 'echo "brew $*" >> "$TEST_CALLS"\n')
    _executable(binaries / "sleep", "/bin/sleep 0.01\n")
    env = {
        **os.environ,
        "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
        "SIO_DATA_DIR": str(tmp_path / "state"),
        "TEST_CALLS": str(tmp_path / "calls"),
        "TEST_STARTED": str(tmp_path / "started"),
    }
    return scripts / "services.sh", env


def _start_redis(script: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), "start", "redis"],
        env=env,
        capture_output=True,
        text=True,
        # Startup permits a 20-second readiness wait. Keep the process guard above that
        # budget so loaded runners can finish and report the actual startup outcome.
        timeout=30,
        check=False,
    )


def test_reuses_healthy_redis_invisible_to_lsof(service_sandbox) -> None:
    script, env = service_sandbox
    result = _start_redis(script, env)
    assert result.returncode == 0, result.stderr
    assert "already running" in result.stdout
    assert not Path(env["TEST_STARTED"]).exists()
    assert not (Path(env["SIO_DATA_DIR"]) / "run" / "redis.pid").exists()
    calls = Path(env["TEST_CALLS"]).read_text()
    assert "nc -z -w 1 127.0.0.1 6379" in calls
    assert "ping - 6379" in calls
    assert "lsof" not in calls


def test_does_not_start_over_unhealthy_or_unrelated_listener(service_sandbox) -> None:
    script, env = service_sandbox
    result = _start_redis(script, {**env, "TEST_PING_STATUS": "1"})
    assert result.returncode == 1
    assert "leaving the existing listener unchanged" in result.stderr
    assert not Path(env["TEST_STARTED"]).exists()
    assert not (Path(env["SIO_DATA_DIR"]) / "run" / "redis.pid").exists()


@pytest.mark.parametrize("ping_status", ["0", "1"])
def test_new_daemon_requires_redis_ping(service_sandbox, ping_status: str) -> None:
    script, env = service_sandbox
    result = _start_redis(script, {**env, "TEST_PORT_OPEN": "0", "TEST_PING_STATUS": ping_status})
    assert Path(env["TEST_STARTED"]).exists()
    calls = Path(env["TEST_CALLS"]).read_text().splitlines()
    assert calls.count("nc -z -w 1 127.0.0.1 6379") >= 2
    assert "ping - 6379" in calls
    assert result.returncode == int(ping_status)
    if ping_status == "0":
        assert "PING successful" in result.stdout
    else:
        assert "did not return PONG" in result.stderr


@pytest.mark.parametrize("ping_status", ["0", "1"])
def test_macos_brew_start_also_requires_redis_ping(service_sandbox, ping_status: str) -> None:
    script, env = service_sandbox
    result = _start_redis(
        script, {**env, "TEST_PLATFORM": "Darwin", "TEST_PING_STATUS": ping_status}
    )
    assert result.returncode == int(ping_status)
    assert "brew services start" in Path(env["TEST_CALLS"]).read_text()
    assert not Path(env["TEST_STARTED"]).exists()


@pytest.mark.parametrize(
    ("available", "nc_status", "expected_status", "expected_call"),
    [
        ("both", 1, 1, "nc"),
        ("lsof", 1, 0, "lsof"),
        ("none", 1, 1, ""),
    ],
)
def test_port_probe_fallbacks(available, nc_status, expected_status, expected_call) -> None:
    result = subprocess.run(
        [
            "bash",
            "-c",
            ". scripts/lib.sh\n"
            'have() { [ "$TEST_AVAILABLE" = both ] || '
            '{ [ "$TEST_AVAILABLE" = lsof ] && [ "$1" = lsof ]; }; }\n'
            "lsof() { echo lsof >&3; return 0; }\n"
            'nc() { echo nc >&3; return "$TEST_NC_STATUS"; }\n'
            "port_in_use 6379 3>&1\n",
        ],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "TEST_AVAILABLE": available,
            "TEST_NC_STATUS": str(nc_status),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == expected_status
    assert result.stdout.strip() == expected_call


@pytest.mark.parametrize(
    ("reply", "error", "expected_status"),
    [
        (b"+PONG\r\n", None, 0),
        (b"-NOAUTH Authentication required.\r\n", None, 1),
        (b"HTTP/1.1 200 OK\r\n", None, 1),
        (b"+PONG-extra\r\n", None, 1),
        (b"", None, 1),
        (b"", TimeoutError(), 1),
        (b"", ConnectionRefusedError(), 1),
    ],
)
def test_redis_health_checks_bounded_protocol_reply(monkeypatch, reply, error, expected_status):
    """Run the exact embedded health probe against an in-memory socket double."""
    source = (REPO_ROOT / "scripts" / "services.sh").read_text()
    match = re.search(r"python3 - .*? <<'PY'\n(.*?)\nPY", source, re.S)
    assert match is not None
    sent: list[bytes] = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def sendall(self, data):
            sent.append(data)

        def makefile(self, mode):
            assert mode == "rb"
            if error is not None:
                raise error
            return io.BytesIO(reply)

    def connect(address, *, timeout):
        assert address == ("127.0.0.1", 6379)
        assert timeout == 1
        return Connection()

    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(sys, "argv", ["-", "6379"])
    with pytest.raises(SystemExit) as raised:
        exec(compile(match.group(1), str(REPO_ROOT / "scripts" / "services.sh"), "exec"), {})
    assert raised.value.code == expected_status
    assert sent == [b"PING\r\n"]
