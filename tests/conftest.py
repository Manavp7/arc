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
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
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
def memory_bus() -> Iterator[object]:
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
def sample_geo() -> object:
    """A point inside the demo yard (see infra/site/yard.geojson)."""
    from sio_schemas import Geo

    return Geo(lat=37.7749, lon=-122.4194)


# --- governance ---------------------------------------------------------------------------------
#
# Phase 5 made a principal mandatory on every endpoint, which immediately broke twenty tests that were
# calling routes with no token. That was the enforcement working, not a regression — but it means every
# test touching HTTP now needs a token, and twenty copies of the token-minting code would be twenty places
# to keep in step.
#
# So: two fixtures. `bearer` mints a token with whichever roles a test needs, and `client_for` builds an
# authenticated TestClient. A test that wants to prove a DENIAL asks for no token and gets one, which is
# the point — the negative tests must be as easy to write as the positive ones, or they will not be written.


@pytest.fixture
def bearer():
    """Mint a dev bearer token with chosen roles, clearance, zones and PII scope."""
    from sio_core.authn import DevJwtAuth

    issuer = DevJwtAuth()

    def mint(
        *,
        subject: str = "tester",
        tenant_id: str | None = None,
        roles: tuple[str, ...] = ("admin",),
        clearance: int = 3,
        zones: tuple[str, ...] = (),
        pii_scope: bool = False,
    ) -> str:
        return issuer.issue(
            subject=subject,
            tenant_id=tenant_id,
            roles=roles,
            clearance=clearance,
            zones=zones,
            pii_scope=pii_scope,
        )

    return mint


@pytest.fixture
def auth_headers(bearer):
    """Headers for an admin principal — the usual case for a test that is not about authorisation."""
    return {"Authorization": f"Bearer {bearer()}"}
