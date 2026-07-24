"""Integration-ring fixtures.

The root ``conftest.py`` forces in-memory adapters so the unit ring never needs
infrastructure. That default must not leak down here — an integration test that quietly ran
against ``MemoryBus`` would pass while proving nothing (and did: the MinIO round-trip test
skipped itself with "blob backend is not minio" until this file existed).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

REAL_BACKENDS = {
    "SIO_BUS_BACKEND": "redis",
    "SIO_GRAPH_BACKEND": "neo4j",
    "SIO_VECTOR_BACKEND": "pgvector",
    "SIO_BLOB_BACKEND": "minio",
}


@pytest.fixture(autouse=True, scope="session")
def _use_real_backends() -> Iterator[None]:
    """Point configuration at the real datastores for the whole integration session."""
    from sio_core.config import reset_settings

    previous = {key: os.environ.get(key) for key in REAL_BACKENDS}
    os.environ.update(REAL_BACKENDS)
    reset_settings()
    yield
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    reset_settings()
