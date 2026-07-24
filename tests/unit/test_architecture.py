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


def test_no_service_reads_os_environ_directly() -> None:
    """Configuration flows through sio_core.config so `just doctor` can report it."""
    offenders: list[str] = []
    for path in python_files(SERVICES_DIR):
        text = path.read_text()
        if "os.environ" in text or "os.getenv" in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        "read configuration from sio_core.config.Settings, not os.environ:\n  "
        + "\n  ".join(offenders)
    )
