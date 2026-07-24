"""Configuration and registry behaviour."""

from __future__ import annotations

from pathlib import Path

import pytest

from sio_core import registry
from sio_core.config import Settings, get_settings, reset_settings
from sio_core.errors import ConfigError


@pytest.fixture
def pristine_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip SIO_/NEO4J_ variables so the *code* defaults are what gets asserted.

    ``tests/conftest.py`` deliberately forces infra-free adapters for the whole session, so
    without this a "defaults" test would only be re-reading the test harness.
    """
    import os

    for key in list(os.environ):
        if key.startswith(("SIO_", "NEO4J_")):
            monkeypatch.delenv(key, raising=False)


def test_defaults_are_local_first(pristine_env: None) -> None:
    cfg = Settings(_env_file=None)  # type: ignore[call-arg]
    assert cfg.bus_backend == "redis"
    assert cfg.graph_backend == "neo4j"
    assert cfg.vector_backend == "pgvector"
    assert cfg.blob_backend == "minio"
    assert cfg.auth_mode == "dev"
    assert cfg.workflow_runner == "temporal"


def test_governance_flags_default_to_the_safe_position(pristine_env: None) -> None:
    """PRD NG2/R4: face recognition off, redaction on, unless someone deliberately changes it."""
    cfg = Settings(_env_file=None)  # type: ignore[call-arg]
    assert cfg.enable_face_recognition is False
    assert cfg.blur_faces is True
    assert cfg.blur_plates is True
    assert cfg.redact_pii is True
    assert cfg.retain_raw is False
    assert cfg.audit_enabled is True
    assert cfg.agent_require_approval is True


def test_sio_prefixed_env_vars_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIO_BUS_BACKEND", "memory")
    monkeypatch.setenv("SIO_DET_CONF", "0.55")
    reset_settings()
    cfg = get_settings()
    assert cfg.bus_backend == "memory"
    assert cfg.det_conf == 0.55
    reset_settings()


def test_neo4j_conventional_env_names_are_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """`NEO4J_PASSWORD` is what every Neo4j doc and the bootstrap script use."""
    monkeypatch.setenv("NEO4J_PASSWORD", "from-conventional-name")
    monkeypatch.setenv("NEO4J_URI", "bolt://db:7687")
    reset_settings()
    cfg = get_settings()
    assert cfg.neo4j_password == "from-conventional-name"
    assert cfg.neo4j_uri == "bolt://db:7687"
    reset_settings()


def test_prefixed_neo4j_names_also_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    monkeypatch.setenv("SIO_NEO4J_PASSWORD", "prefixed")
    reset_settings()
    assert get_settings().neo4j_password == "prefixed"
    reset_settings()


def test_pg_dsn_is_composed() -> None:
    cfg = Settings(
        _env_file=None,  # type: ignore[call-arg]
        pg_user="sio",
        pg_password="secret",
        pg_host="db",
        pg_port=6000,
        pg_database="siodb",
    )
    assert cfg.pg_dsn == "postgresql://sio:secret@db:6000/siodb"


def test_list_valued_settings_are_split() -> None:
    cfg = Settings(
        _env_file=None,  # type: ignore[call-arg]
        cors_origins="http://a, http://b ,",
        agents_enabled="security, logistics",
    )
    assert cfg.cors_origin_list == ["http://a", "http://b"]
    assert cfg.agent_list == ["security", "logistics"]


def test_port_lookup_by_service_name(pristine_env: None) -> None:
    cfg = Settings(_env_file=None)  # type: ignore[call-arg]
    assert cfg.port_for("api") == cfg.api_port
    assert cfg.port_for("perception") == cfg.perception_port
    assert cfg.port_for("world-model-typo") == 0


def test_ports_are_unique_across_services(pristine_env: None) -> None:
    """Two services on one port is a boot failure that only shows up under `just dev`."""
    cfg = Settings(_env_file=None)  # type: ignore[call-arg]
    names = [
        "api",
        "ingest",
        "perception",
        "tracking",
        "fusion",
        "worldmodel",
        "spatial",
        "events",
        "prediction",
        "simulation",
        "decision",
        "copilot",
        "mcp",
        "agents",
        "workflow",
        "alerts",
        "missions",
        "analytics",
        "governance",
    ]
    ports = [cfg.port_for(name) for name in names]
    assert len(set(ports)) == len(ports), f"duplicate service port: {ports}"


def test_ensure_dirs_creates_runtime_layout(tmp_path: Path) -> None:
    cfg = Settings(_env_file=None, data_dir=tmp_path / "state")  # type: ignore[call-arg]
    cfg.ensure_dirs()
    for path in (cfg.data_dir, cfg.logs_dir, cfg.run_dir, cfg.model_dir, cfg.samples_dir):
        assert path.exists(), path


def test_adapter_summary_covers_every_seam(pristine_env: None) -> None:
    summary = Settings(_env_file=None).adapter_summary()  # type: ignore[call-arg]
    assert set(summary) == {
        "bus",
        "graph",
        "vector",
        "blob",
        "detector",
        "tracker",
        "embedder",
        "forecaster",
        "llm",
        "auth",
        "policy",
        "workflow",
        "cep",
    }


def test_model_path_resolves_under_the_model_dir() -> None:
    cfg = Settings(_env_file=None, model_dir=Path(".sio/models"))  # type: ignore[call-arg]
    assert cfg.model_path("yolo26n.onnx") == Path(".sio/models/yolo26n.onnx")


# ------------------------------------------------------------------------ registry
def test_registry_returns_the_configured_adapters() -> None:
    cfg = Settings(
        _env_file=None,  # type: ignore[call-arg]
        bus_backend="memory",
        graph_backend="memory",
        vector_backend="memory",
        blob_backend="file",
    )
    from sio_core.bus.memory import MemoryBus
    from sio_core.stores.graph_memory import MemoryGraphStore

    assert isinstance(registry.get_bus(cfg), MemoryBus)
    assert isinstance(registry.get_graph(cfg), MemoryGraphStore)


def test_registry_caches_instances() -> None:
    cfg = Settings(_env_file=None, bus_backend="memory")  # type: ignore[call-arg]
    assert registry.get_bus(cfg) is registry.get_bus(cfg), "pools must be shared, not re-created"


def test_registry_override_wins_even_before_first_use() -> None:
    """Tests must be able to inject a fake before any real adapter is constructed."""
    sentinel = object()
    registry.override("bus", sentinel)
    cfg = Settings(_env_file=None, bus_backend="redis")  # type: ignore[call-arg]
    assert registry.get_bus(cfg) is sentinel


def test_unknown_backend_is_a_config_error() -> None:
    cfg = Settings(_env_file=None)  # type: ignore[call-arg]
    object.__setattr__(cfg, "bus_backend", "carrier-pigeon")
    with pytest.raises(ConfigError, match="carrier-pigeon"):
        registry.get_bus(cfg)


def test_phase_7_backends_fail_with_a_useful_message() -> None:
    cfg = Settings(_env_file=None)  # type: ignore[call-arg]
    object.__setattr__(cfg, "bus_backend", "kafka")
    with pytest.raises(ConfigError, match="Phase 7"):
        registry.get_bus(cfg)
