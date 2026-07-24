"""Store adapters. Import via :mod:`sio_core.registry`, not directly from a service."""

from __future__ import annotations

from .blob import FileBlobStore, MinioBlobStore
from .graph_memory import MemoryGraphStore
from .graph_neo4j import Neo4jGraphStore
from .graph_pg import PostgresGraphStore
from .pg import PgPool
from .vectors import DEFAULT_DIM, MemoryVectorStore, PgVectorStore, cosine_similarity

__all__ = [
    "DEFAULT_DIM",
    "FileBlobStore",
    "MemoryGraphStore",
    "MemoryVectorStore",
    "MinioBlobStore",
    "Neo4jGraphStore",
    "PgPool",
    "PgVectorStore",
    "PostgresGraphStore",
    "cosine_similarity",
]
