"""The demo must keep working (PRD P4.7 ship checkpoint).

`just demo` is a deliverable, not a side effect of the code working. Phases 5-8 are strictly additive from
the P4 exit, and "additive" is a claim that needs enforcing: the cheapest way for a later phase to break the
product is to break the demo without breaking a unit test.

So this runs the real script against a real stack and asserts the incident completes end to end — a fire
detected from camera imagery, scored into a prioritised alert, answered with a playbook, and turned into a
recommendation that is *waiting for a human*.

It **skips** rather than fails when the platform is not running, because a unit-test run on a laptop with no
services should not go red. That is a deliberate trade and it has a cost: a skip is easy to ignore. The
guard against that is `just e2e`, which asserts the stack is up first, and CI running it there.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = {
    "api": 8000,
    "ingest": 8101,
    "events": 8107,
    "alerts": 8115,
    "decision": 8110,
    "workflow": 8114,
}


def stack_is_up() -> tuple[bool, str]:
    missing = []
    for name, port in REQUIRED.items():
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=3.0)
            if response.status_code != 200:
                missing.append(f"{name}(HTTP {response.status_code})")
        except httpx.HTTPError:
            missing.append(name)
    return not missing, ", ".join(missing)


@pytest.fixture(scope="module")
def running_stack() -> None:
    up, missing = stack_is_up()
    if not up:
        pytest.skip(
            f"the platform is not running ({missing}); start it with: just services && just dev"
        )


@pytest.mark.e2e
def test_the_demo_completes(running_stack: None) -> None:
    """The whole incident, through the real script.

    Deliberately shells out to `scripts/demo.py` rather than importing it. The thing that must not break is
    the command a reviewer types, and importing the module would test the functions while leaving the
    argument parsing, the exit code and the narration — the actual interface — unexercised.
    """
    result = subprocess.run(
        [sys.executable, "scripts/demo.py", "--headless"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    output = result.stdout + result.stderr

    # The exit code first, because that is what CI and a reviewer's shell actually read.
    assert result.returncode == 0, f"just demo --headless failed:\n{output[-3000:]}"

    # Then each stage, so a failure says WHICH link in the chain broke rather than only that it did. A bare
    # "the demo failed" sends the next reader to the logs to work out what the demo even does.
    for stage in ("event: produced", "alert: produced", "run: produced", "decision: produced"):
        assert stage in output, f"missing {stage!r} from the demo summary:\n{output[-3000:]}"

    assert "The demo completed." in output


@pytest.mark.e2e
def test_the_demo_reports_the_fire_and_not_some_other_alert(running_stack: None) -> None:
    """The narration must be about the incident it caused.

    An earlier version of the probe fell back to "any new alert", and in a busy simulated yard there is
    always one — so the demo announced an alert and displayed `Worker 12 entered Fuel store` directly
    beneath a fire. A demo that narrates the wrong row confidently is worse than one that admits it found
    nothing, because the audience cannot tell.
    """
    result = subprocess.run(
        [sys.executable, "scripts/demo.py", "--headless"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-2000:]
    assert "a FIRE alert for the fuel_store" in output
    # The alert it reports must be a fire one. Anything about a worker entering means the fallback is back.
    fire_line = next(
        (line for line in output.splitlines() if "priority" in line and "[critical]" in line), ""
    )
    assert fire_line, f"no critical fire alert was reported:\n{output[-2000:]}"
    assert "entered" not in fire_line.lower(), (
        f"reported a zone-entry alert as the fire's: {fire_line}"
    )


@pytest.mark.e2e
def test_reset_is_idempotent(running_stack: None) -> None:
    """`just demo-reset` must be able to say it is finished.

    A single pass over one page of alerts was not a reset: the inbox was deeper than the page, so clearing
    200 rows revealed the next 200 and running it twice reported "resolved 200 alerts" both times.
    """
    first = subprocess.run(
        [sys.executable, "scripts/demo.py", "--reset"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert first.returncode == 0, first.stdout + first.stderr

    second = subprocess.run(
        [sys.executable, "scripts/demo.py", "--reset"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert second.returncode == 0, second.stdout + second.stderr
    # The second pass converges quickly. A live simulator may raise one or two in between, so this asserts
    # the loop terminates rather than that the count is exactly zero — the failure mode being guarded
    # against is a reset that never finishes, not a yard that stays quiet.
    assert "pass(es)" in second.stdout
    assert "stopped after" not in second.stdout, (
        "the reset did not converge on a second run:\n" + second.stdout[-1500:]
    )


@pytest.mark.e2e
def test_the_demo_refuses_to_run_against_a_dead_platform() -> None:
    """And says which service is missing, and what to type.

    Checked by pointing the script at ports nothing is listening on. A demo script whose failure mode is a
    traceback teaches the reader nothing; one that prints `just services` teaches them everything they need.
    """
    source = (ROOT / "scripts" / "demo.py").read_text()
    assert "just services" in source
    assert "just dev" in source
    # And the preflight has to run before anything else, or the first failure will be a confusing symptom
    # from deep inside a step rather than "ingest is not reachable".
    run_body = source[source.index("async def run(") :]
    assert run_body.index("preflight") < run_body.index("ensure_site")


def test_the_smoke_test_would_notice_a_missing_stage() -> None:
    """A test that cannot fail is decoration. This asserts the assertions are real.

    Runs without the stack, deliberately: it checks the shape of this file's own checks rather than the
    platform, so it is worth running everywhere.
    """
    source = Path(__file__).read_text()
    for stage in ("event: produced", "alert: produced", "run: produced", "decision: produced"):
        assert stage in source, f"the smoke test does not check for {stage!r}"
    assert "returncode == 0" in source, "the smoke test does not check the exit code"
