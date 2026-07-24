"""macOS is the supported platform; this test stops the Linux path from quietly taking over.

The development VM used to build SIO runs Ubuntu, which makes it very easy to write a script
that works there and fails on a Mac — GNU-only flags, bash 4 syntax, systemd assumptions.
Those failures surface as "it works on your machine" a week later, so they are caught here
instead.

The rules:
  1. no GNU-only invocations (``sed -i`` without a suffix, ``readlink -f``, ``date -d``,
     ``grep -P``, ``mktemp -d --tmpdir``, ``cp --parents``, ``stat -c``);
  2. no bash-4 features (associative arrays, ``mapfile``, ``${var,,}``) — macOS ships bash 3.2;
  3. platform-specific commands (``apt-get``, ``brew``, ``systemctl``, ``dpkg``) only inside a
     platform branch;
  4. never kill by name (``pkill``/``killall``) — SIO stops processes by recorded pid;
  5. nothing in the ``just check`` path may require Linux-only tooling.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = sorted((REPO_ROOT / "scripts").glob("*.sh"))

GNU_ONLY = [
    (re.compile(r"\bsed\s+-i\s+(?!['\"]?\.)"), "sed -i needs a backup suffix on BSD/macOS: sed -i ''"),
    (re.compile(r"\breadlink\s+-f\b"), "readlink -f is GNU-only; use cd/pwd or python"),
    (re.compile(r"\bdate\s+-d\b"), "date -d is GNU-only; use date -r or python"),
    (re.compile(r"\bgrep\s+-P\b"), "grep -P is GNU-only; use grep -E"),
    (re.compile(r"\bstat\s+-c\b"), "stat -c is GNU-only; use wc -c or python"),
    (re.compile(r"\bcp\s+--parents\b"), "cp --parents is GNU-only"),
    (re.compile(r"\bmktemp\s+-d\s+--tmpdir\b"), "mktemp --tmpdir is GNU-only"),
    (re.compile(r"\btac\b"), "tac is GNU-only; use tail -r or sort -r"),
    (re.compile(r"\bfree\s+-[a-z]"), "free(1) does not exist on macOS"),
    (re.compile(r"\bnproc\b"), "nproc is GNU-only; use sysctl -n hw.ncpu on macOS"),
]

BASH4_ONLY = [
    (re.compile(r"\bdeclare\s+-A\b"), "associative arrays need bash 4; macOS ships bash 3.2"),
    (re.compile(r"\bmapfile\b|\breadarray\b"), "mapfile needs bash 4"),
    (re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*,,\}"), "${var,,} lowercasing needs bash 4"),
    (re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\^\^\}"), "${var^^} uppercasing needs bash 4"),
]

PLATFORM_COMMANDS = ("apt-get", "dpkg", "systemctl", "brew", "sw_vers", "sysctl")

NEVER = [
    (re.compile(r"\bpkill\b"), "never kill by name; stop processes by recorded pid"),
    (re.compile(r"\bkillall\b"), "never kill by name; stop processes by recorded pid"),
]


def script_files() -> list[Path]:
    assert SCRIPTS, "expected shell scripts under scripts/"
    return SCRIPTS


def code_lines(path: Path) -> list[tuple[int, str]]:
    """Lines with comments stripped, so documentation may discuss forbidden constructs."""
    out: list[tuple[int, str]] = []
    for number, raw in enumerate(path.read_text().splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        code = raw.split(" #", 1)[0]
        out.append((number, code))
    return out


@pytest.mark.parametrize("path", script_files(), ids=lambda p: p.name)
def test_no_gnu_only_constructs(path: Path) -> None:
    offenders = [
        f"{path.name}:{number}: {reason} -> {line.strip()}"
        for number, line in code_lines(path)
        for pattern, reason in GNU_ONLY
        if pattern.search(line)
    ]
    assert not offenders, "GNU-only shell constructs break the macOS path:\n  " + "\n  ".join(
        offenders
    )


@pytest.mark.parametrize("path", script_files(), ids=lambda p: p.name)
def test_no_bash4_only_constructs(path: Path) -> None:
    offenders = [
        f"{path.name}:{number}: {reason} -> {line.strip()}"
        for number, line in code_lines(path)
        for pattern, reason in BASH4_ONLY
        if pattern.search(line)
    ]
    assert not offenders, "macOS ships bash 3.2:\n  " + "\n  ".join(offenders)


@pytest.mark.parametrize("path", script_files(), ids=lambda p: p.name)
def test_never_kills_by_name(path: Path) -> None:
    offenders = [
        f"{path.name}:{number}: {reason}"
        for number, line in code_lines(path)
        for pattern, reason in NEVER
        if pattern.search(line)
    ]
    assert not offenders, "\n  ".join(offenders)


FUNCTION_DEF = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{")
PLATFORM_GUARD = re.compile(r"is_macos|is_linux|uname -s|Darwin\)|Linux\)")


@pytest.mark.parametrize("path", script_files(), ids=lambda p: p.name)
def test_platform_commands_are_branch_gated(path: Path) -> None:
    """``apt-get``/``brew``/``systemctl`` may only run inside a platform branch.

    Checked by enclosing scope rather than by line order, because shell functions are *defined*
    in one order and *called* in another: ``start_postgres_macos()`` legitimately contains
    ``brew`` at the top of a file whose ``is_macos`` dispatch is at the bottom. A command is
    acceptable when its enclosing function is platform-named (``*_macos``/``*_linux``), or when
    a platform guard appears between the function's opening brace and the command.
    """
    lines = path.read_text().splitlines()
    offenders: list[str] = []
    current_function: str | None = None
    function_start = 0

    for index, raw in enumerate(lines):
        if raw.strip().startswith("#"):
            continue
        line = raw.split(" #", 1)[0]

        match = FUNCTION_DEF.match(line)
        if match:
            current_function = match.group(1)
            function_start = index
            continue
        if line.startswith("}"):
            current_function = None

        for command in PLATFORM_COMMANDS:
            if not re.search(rf"\b{command}\b", line):
                continue
            if current_function and re.search(r"(macos|darwin|linux)", current_function, re.I):
                continue  # platform-named function: the branch *is* the function
            scope = "\n".join(lines[function_start : index + 1])
            if PLATFORM_GUARD.search(scope):
                continue  # guarded within the same scope
            if PLATFORM_GUARD.search("\n".join(lines[max(0, index - 3) : index + 1])):
                continue  # guarded on an immediately preceding line
            offenders.append(f"{path.name}:{index + 1}: unguarded {command} -> {line.strip()}")

    assert not offenders, (
        "platform-specific commands must sit behind is_macos/is_linux:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("path", script_files(), ids=lambda p: p.name)
def test_scripts_are_executable_bash_with_strict_mode(path: Path) -> None:
    text = path.read_text()
    assert text.startswith("#!/usr/bin/env bash"), f"{path.name} must use the env bash shebang"
    assert re.search(r"set -[a-z]*u", text), f"{path.name} must enable nounset"
    assert "pipefail" in text, f"{path.name} must enable pipefail"
    # errexit is the default expectation, but a script whose whole job is to keep going and
    # report every problem (the doctor) must be able to opt out — explicitly, and with a reason.
    if not re.search(r"set -[a-z]*e", text):
        assert "no-errexit:" in text, (
            f"{path.name} does not enable errexit and does not explain why "
            "(add a '# no-errexit: <reason>' comment)"
        )
    assert path.stat().st_mode & 0o111, f"{path.name} is not executable (chmod +x)"


def test_bootstrap_keeps_macos_as_the_default_branch() -> None:
    """The Darwin branch must come first and must not be reachable only via a Linux fallback."""
    text = (REPO_ROOT / "scripts" / "bootstrap.sh").read_text()
    macos_index = text.index("bootstrap_macos_datastores()")
    linux_index = text.index("bootstrap_linux_datastores()")
    assert macos_index < linux_index, "the macOS path is the supported one; keep it first"
    dispatch = text[text.index("if is_macos; then") :]
    assert "bootstrap_macos_datastores" in dispatch.split("elif")[0], (
        "is_macos must dispatch to the macOS bootstrap"
    )


def test_check_recipe_uses_no_linux_only_tooling() -> None:
    """``just check`` is the phase gate and must pass on the reviewer's Mac."""
    justfile = (REPO_ROOT / "Justfile").read_text()
    body: list[str] = []
    capture = False
    for line in justfile.splitlines():
        if re.match(r"^(check|lint|typecheck|test|web-check)\b", line):
            capture = True
            continue
        if capture:
            if line and not line[0].isspace():
                capture = False
                continue
            body.append(line)
    joined = "\n".join(body)
    for forbidden in ("apt-get", "systemctl", "dpkg", "docker"):
        assert forbidden not in joined, f"just check must not depend on {forbidden}"


def test_justfile_documents_every_public_recipe() -> None:
    """A recipe nobody can discover may as well not exist."""
    lines = (REPO_ROOT / "Justfile").read_text().splitlines()
    undocumented: list[str] = []
    for index, line in enumerate(lines):
        match = re.match(r"^([a-z][a-z0-9-]*)(\s+\*?args)?:", line)
        if not match:
            continue
        name = match.group(1)
        if name.startswith("_"):
            continue
        previous = lines[index - 1].strip() if index else ""
        if not previous.startswith("#"):
            undocumented.append(name)
    allowed = {"api", "web", "fmt", "lint", "typecheck", "test-all", "demo-reset",
               "services-stop", "services-status", "services-restart", "web-check"}
    unexpected = set(undocumented) - allowed
    assert not unexpected, f"add a comment above these recipes: {sorted(unexpected)}"
