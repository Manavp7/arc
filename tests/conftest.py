"""Shared fixtures.

Ring 1 (``tests/unit``) must run with **no infrastructure at all**, on any laptop, in seconds.
That is enforced here by pointing every adapter selector at its in-memory implementation
before ``sio_core.config`` is ever read.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def pytest_configure(config: pytest.Config) -> None:
    """Force infra-free defaults for the whole session before settings are cached."""
    os.environ.setdefault("SIO_BUS_BACKEND", "memory")
    os.environ.setdefault("SIO_GRAPH_BACKEND", "memory")
    os.environ.setdefault("SIO_VECTOR_BACKEND", "memory")
    os.environ.setdefault("SIO_BLOB_BACKEND", "file")
    os.environ.setdefault("SIO_LLM_PROVIDER", "scripted")
    os.environ.setdefault("SIO_WORKFLOW_RUNNER", "inline")
    os.environ.setdefault("SIO_LOG_LEVEL", "WARNING")
    os.environ.setdefault("SIO_METRICS_ENABLED", "true")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip infra/e2e rings unless the operator opted in with ``SIO_TEST_INFRA=1``."""
    if os.environ.get("SIO_TEST_INFRA") == "1":
        return
    skip = pytest.mark.skip(reason="requires live infrastructure; set SIO_TEST_INFRA=1")
    for item in items:
        if "infra" in item.keywords or "e2e" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator["object"]:
    """Fresh :class:`~sio_core.config.Settings` writing all state under ``tmp_path``."""
    from sio_core.config import Settings, reset_settings

    monkeypatch.setenv("SIO_DATA_DIR", str(tmp_path / "sio"))
    reset_settings()
    cfg = Settings(
        data_dir=tmp_path / "sio",
        bus_backend="memory",
        graph_backend="memory",
        vector_backend="memory",
        blob_backend="file",
        metrics_enabled=True,
    )
    cfg.ensure_dirs()
    yield cfg
    reset_settings()


@pytest.fixture
def memory_bus() -> Iterator["object"]:
    from sio_core.bus.memory import MemoryBus

    bus = MemoryBus()
    yield bus


@pytest.fixture(autouse=True)
def _reset_registry() -> Iterator[None]:
    """Never let one test's adapter leak into the next."""
    from sio_core import registry

    registry.reset()
    yield
    registry.reset()


@pytest.fixture
def sample_geo() -> "object":
    """A point inside the demo yard (see infra/site/yard.geojson)."""
    from sio_schemas import Geo

    return Geo(lat=37.7749, lon=-122.4194)
