"""Architectural fitness tests.

The PRD's promise that a CPU stack becomes a GPU stack by changing environment variables
(§9.3) only survives if services depend on *ports*, never on adapters. Conventions decay;
this test does not. It walks the AST of every service and library and fails the build on a
forbidden import.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICES_DIR = REPO_ROOT / "services"
LIBS_DIR = REPO_ROOT / "libs"

# Modules a service must not import directly: they are concrete adapters, reachable only
# through sio_core.registry.
FORBIDDEN_FOR_SERVICES = (
    "sio_core.bus.memory",
    "sio_core.bus.redis_bus",
    "sio_core.stores.graph_neo4j",
    "sio_core.stores.graph_pg",
    "sio_core.stores.graph_memory",
    "sio_core.stores.vectors",
    "sio_core.stores.blob",
    "sio_core.stores.pg",
)

# Third-party drivers that must only ever be touched inside an adapter.
FORBIDDEN_DRIVERS = ("redis", "neo4j", "minio", "psycopg", "psycopg_pool")

ALLOWED_DRIVER_MODULES = {
    "sio_core.bus.redis_bus": {"redis"},
    "sio_core.stores.graph_neo4j": {"neo4j"},
    "sio_core.stores.blob": {"minio"},
    "sio_core.stores.pg": {"psycopg", "psycopg_pool"},
}


def python_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [p for p in root.rglob("*.py") if ".venv" not in p.parts and "build" not in p.parts]


def imported_modules(path: Path) -> set[str]:
    """Every module name imported by ``path``, including ``from x.y import z`` as ``x.y``."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            for alias in node.names:
                found.add(f"{node.module}.{alias.name}")
    return found


def module_name_for(path: Path) -> str:
    """Best-effort dotted module name for a file inside ``libs/*/src``."""
    parts = list(path.with_suffix("").parts)
    if "src" in parts:
        parts = parts[parts.index("src") + 1 :]
    return ".".join(parts)


def test_services_never_import_adapters_directly() -> None:
    offenders: list[str] = []
    for path in python_files(SERVICES_DIR):
        for imported in imported_modules(path):
            for forbidden in FORBIDDEN_FOR_SERVICES:
                if imported == forbidden or imported.startswith(f"{forbidden}."):
                    offenders.append(f"{path.relative_to(REPO_ROOT)} imports {imported}")
    assert not offenders, (
        "services must obtain adapters from sio_core.registry, not import them:\n  "
        + "\n  ".join(offenders)
    )


def test_services_never_import_infrastructure_drivers() -> None:
    offenders: list[str] = []
    for path in python_files(SERVICES_DIR):
        for imported in imported_modules(path):
            root = imported.split(".")[0]
            if root in FORBIDDEN_DRIVERS:
                offenders.append(f"{path.relative_to(REPO_ROOT)} imports {imported}")
    assert not offenders, (
        "infrastructure drivers belong inside sio_core adapters:\n  " + "\n  ".join(offenders)
    )


def test_drivers_are_confined_to_their_adapter_modules() -> None:
    """Even inside sio_core, only the owning adapter may import its driver."""
    offenders: list[str] = []
    for path in python_files(LIBS_DIR):
        module = module_name_for(path)
        allowed = ALLOWED_DRIVER_MODULES.get(module, set())
        for imported in imported_modules(path):
            root = imported.split(".")[0]
            if root in FORBIDDEN_DRIVERS and root not in allowed:
                offenders.append(f"{path.relative_to(REPO_ROOT)} imports {imported}")
    assert not offenders, "driver import outside its adapter:\n  " + "\n  ".join(offenders)


def test_schemas_library_stays_dependency_light() -> None:
    """sio_schemas is imported by everything, including tools; it must stay tiny."""
    allowed_roots = {
        "pydantic",
        "typing",
        "datetime",
        "enum",
        "json",
        "os",
        "secrets",
        "time",
        "argparse",
        "pathlib",
        "math",
        "collections",
        "__future__",
        "sio_schemas",
        "typing_extensions",
    }
    offenders: list[str] = []
    for path in python_files(LIBS_DIR / "sio_schemas"):
        for imported in imported_modules(path):
            root = imported.split(".")[0]
            if root.startswith("."):
                continue
            if root not in allowed_roots:
                offenders.append(f"{path.relative_to(REPO_ROOT)} imports {imported}")
    assert not offenders, "sio_schemas must not grow dependencies:\n  " + "\n  ".join(offenders)


def test_schemas_does_not_depend_on_core() -> None:
    """The dependency arrow points one way: core → schemas."""
    for path in python_files(LIBS_DIR / "sio_schemas"):
        assert not any(imported.startswith("sio_core") for imported in imported_modules(path)), (
            f"{path} imports sio_core"
        )


def test_every_service_declares_a_pyproject_and_readme() -> None:
    """PRD §10: every service is an isolated uv project with a README."""
    if not SERVICES_DIR.exists():
        pytest.skip("no services yet (Phase 0)")
    for service in sorted(p for p in SERVICES_DIR.iterdir() if p.is_dir()):
        assert (service / "pyproject.toml").exists(), f"{service.name} has no pyproject.toml"
        assert (service / "README.md").exists(), f"{service.name} has no README.md"


#: The marker that makes an `os.environ` line deliberate rather than an oversight.
#:
#: The rule below is a blanket string search, and it should stay one — the moment it starts parsing intent it
#: stops catching the thing it exists to catch. But a blanket rule needs an escape hatch that is *visible*, and
#: a magic comment is greppable in a way an allowlist buried in a test file is not.
ENV_ESCAPE = "SIO-ENV-OK"


def test_no_service_reads_os_environ_directly() -> None:
    """Configuration flows through sio_core.config so `just doctor` can report it.

    Blanket, per line, with one escape: a line tagged `# SIO-ENV-OK: <reason>` is allowed. That exists because
    the RTSP connector has to *write* `OPENCV_FFMPEG_CAPTURE_OPTIONS` — an environment variable is FFmpeg's only
    channel for the RTSP transport, and the value itself comes from `options.transport`, which is configuration
    arriving the proper way. Setting a third-party library's knob is a different act from reading our own config,
    which is what this rule is really about.

    Checked per LINE rather than per file, which is stricter than the first version: a file containing one
    justified write no longer gets a pass for an unjustified read three hundred lines later.
    """
    offenders: list[str] = []
    for path in python_files(SERVICES_DIR):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if "os.environ" not in line and "os.getenv" not in line:
                continue
            if ENV_ESCAPE in line:
                continue
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")
    assert not offenders, (
        "read configuration from sio_core.config.Settings, not os.environ. If a line genuinely must "
        f"touch the environment, tag it `# {ENV_ESCAPE}: <reason>`:\n  " + "\n  ".join(offenders)
    )


def test_the_environment_escape_hatch_is_barely_used() -> None:
    """An escape hatch nobody counts becomes the normal path.

    Two is the current number and there is no good reason for it to grow: every additional one is a piece of
    configuration that `just doctor` cannot report on. If this fails, the question is whether the new case
    belongs in `Settings` rather than whether to raise the number.
    """
    tagged = [
        f"{path.relative_to(REPO_ROOT)}:{number}"
        for path in python_files(SERVICES_DIR)
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if ENV_ESCAPE in line
    ]
    assert len(tagged) <= 2, (
        "the environment escape hatch is spreading; each of these is configuration `just doctor` "
        "cannot see:\n  " + "\n  ".join(tagged)
    )


def test_no_sql_tests_a_bare_placeholder_for_null() -> None:
    """Postgres cannot infer a type for a bare placeholder in an ``IS NULL`` test.

    A lint rather than another bug report, because this one has now cost twice: the world model's track
    insert failed on EVERY track for two phases (contained by the dead-letter queue, so nothing looked
    wrong), and the prediction service's backtest endpoint returned a 500 on its first request. Both were
    one missing ``::text``.

    The idiom itself is fine; it just has to say what type it is.
    """
    import re

    offenders: list[str] = []
    pattern = re.compile(r"%s\s+IS\s+(?:NOT\s+)?NULL", re.IGNORECASE)
    searched = [
        *python_files(SERVICES_DIR),
        *python_files(REPO_ROOT / "libs"),
    ]
    for path in searched:
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith(("#", "--")):
                continue  # a comment describing the mistake is not the mistake
            if pattern.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")
    assert not offenders, (
        "bare placeholder in an IS NULL test; add an explicit cast such as %s::text:\n  "
        + "\n  ".join(offenders)
    )


def test_no_explanation_note_opens_with_a_conjunction() -> None:
    """An explanation note is a standalone claim, not a clause.

    The zone-entry explanation read as two bullets:

        - position was inside the Staging area polygon by more than 2 m
        - and held there for 2 s, so a vehicle clipping the corner while turning does not count

    One sentence split in two, so the second bullet opened with "and". In a list an operator is meant to
    trust, that reads as a formatting accident — and it invites the reader to wonder what else in the
    explanation was assembled carelessly.
    """
    import re

    root = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    pattern = re.compile(r"""add_note\(\s*f?["'](and|but|or|so|because|which)\b""", re.IGNORECASE)
    for path in sorted(root.glob("services/*/src/**/*.py")) + sorted(
        root.glob("libs/*/src/**/*.py")
    ):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(root)}:{number}: {line.strip()[:90]}")
    assert not offenders, "these notes are clauses, not claims:\n" + "\n".join(offenders)
