"""Single source of configuration truth, loaded from the environment and ``.env``.

Every knob in the platform lands here. Services never read ``os.environ`` directly, so
``just doctor`` can report the *effective* configuration and tests can override it with a
plain constructor call.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

BusBackend = Literal["redis", "memory", "kafka"]
GraphBackend = Literal["neo4j", "postgres", "memory"]
VectorBackend = Literal["pgvector", "memory", "qdrant"]
BlobBackend = Literal["minio", "file"]
DetectorKind = Literal["auto", "onnx", "onnx_seg", "synthetic", "null", "deepstream"]
TrackerKind = Literal["bytetrack", "geo", "boxmot", "deepstream"]
EmbedderKind = Literal["clip", "hash"]
ForecasterKind = Literal["statsforecast", "naive", "timesfm"]
LlmProvider = Literal["ollama", "openai_compat", "scripted"]
AuthMode = Literal["dev", "keycloak"]
PolicyEngineKind = Literal["embedded", "opa", "openfga"]
WorkflowRunnerKind = Literal["temporal", "inline"]
CepRuntime = Literal["native", "bytewax"]


def _neo4j(name: str) -> AliasChoices:
    """Accept both the conventional ``NEO4J_*`` name and a prefixed ``SIO_NEO4J_*`` name."""
    return AliasChoices(name, f"SIO_{name}")


class Settings(BaseSettings):
    """Effective configuration. See ``.env.example`` for documentation of every field."""

    model_config = SettingsConfigDict(
        env_prefix="SIO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- identity & runtime --------------------------------------------------
    env: str = "local"
    tenant_id: str = "default"
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"
    data_dir: Path = Path(".sio")

    # --- adapter selection (the swappable seams) ----------------------------
    bus_backend: BusBackend = "redis"
    graph_backend: GraphBackend = "neo4j"
    vector_backend: VectorBackend = "pgvector"
    blob_backend: BlobBackend = "minio"
    detector: DetectorKind = "auto"
    tracker: TrackerKind = "bytetrack"
    embedder: EmbedderKind = "clip"
    forecaster: ForecasterKind = "statsforecast"
    llm_provider: LlmProvider = "ollama"
    auth_mode: AuthMode = "dev"
    policy_engine: PolicyEngineKind = "embedded"
    workflow_runner: WorkflowRunnerKind = "temporal"
    cep_runtime: CepRuntime = "native"

    # --- postgres -----------------------------------------------------------
    pg_host: str = "127.0.0.1"
    pg_port: int = 5432
    pg_database: str = "sio"
    pg_user: str = "sio"
    pg_password: str = "sio"
    pg_pool_min: int = 1
    pg_pool_max: int = 8

    # --- redis / bus --------------------------------------------------------
    redis_url: str = "redis://127.0.0.1:6379/0"
    bus_maxlen: int = 100_000
    bus_block_ms: int = 2_000
    bus_batch: int = 64
    bus_claim_idle_ms: int = 60_000
    bus_max_retries: int = 5

    # --- neo4j --------------------------------------------------------------
    neo4j_uri: str = Field(default="bolt://127.0.0.1:7687", validation_alias=_neo4j("NEO4J_URI"))
    neo4j_user: str = Field(default="neo4j", validation_alias=_neo4j("NEO4J_USER"))
    neo4j_password: str = Field(
        default="siolocalpassword", validation_alias=_neo4j("NEO4J_PASSWORD")
    )
    neo4j_database: str = Field(default="neo4j", validation_alias=_neo4j("NEO4J_DATABASE"))

    # --- minio --------------------------------------------------------------
    minio_endpoint: str = "127.0.0.1:9000"
    minio_access_key: str = "sioadmin"
    minio_secret_key: str = "sioadminsecret"
    minio_bucket: str = "sio-media"
    minio_secure: bool = False

    # --- service ports ------------------------------------------------------
    api_port: int = 8000
    ingest_port: int = 8101
    perception_port: int = 8102
    tracking_port: int = 8103
    fusion_port: int = 8104
    worldmodel_port: int = 8105
    spatial_port: int = 8106
    events_port: int = 8107
    prediction_port: int = 8108
    simulation_port: int = 8109
    decision_port: int = 8110
    copilot_port: int = 8111
    mcp_port: int = 8112
    agents_port: int = 8113
    workflow_port: int = 8114
    alerts_port: int = 8115
    missions_port: int = 8116
    analytics_port: int = 8117
    governance_port: int = 8118

    # --- web / api ----------------------------------------------------------
    api_base_url: str = "http://127.0.0.1:8000"
    web_port: int = 5173
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # --- ingest simulator ---------------------------------------------------
    sim_rate: float = 20.0
    sim_trucks: int = 6
    sim_forklifts: int = 3
    sim_people: int = 8
    sim_drones: int = 1
    sim_seed: int = 1337
    sim_site: Path = Path("infra/site/yard.geojson")
    sim_frame_fps: float = 2.0

    # --- perception ---------------------------------------------------------
    model_dir: Path = Path(".sio/models")
    det_model: str = "yolo26n.onnx"
    seg_model: str = "yolo26n-seg.onnx"
    reid_model: str = "yolo26n-reid.onnx"
    clip_vision_model: str = "clip-vision.onnx"
    clip_text_model: str = "clip-text.onnx"
    clip_tokenizer: str = "clip-tokenizer.json"
    det_conf: float = 0.35
    det_imgsz: int = 640
    ort_threads: int = 2
    perception_fps: float = 2.0
    enable_segmentation: bool = False
    enable_ocr: bool = True
    enable_audio: bool = False
    enable_sam: bool = False

    # --- governance flags ---------------------------------------------------
    enable_face_recognition: bool = False
    blur_faces: bool = True
    blur_plates: bool = True
    redact_pii: bool = True
    retain_raw: bool = False
    audit_enabled: bool = True

    # --- tracking -----------------------------------------------------------
    track_max_age: int = 30
    track_min_hits: int = 3
    track_iou_threshold: float = 0.3
    track_reid_threshold: float = 0.75
    enable_cross_camera: bool = True

    # --- fusion -------------------------------------------------------------
    fusion_assoc_radius_m: float = 25.0
    fusion_time_window_s: float = 5.0
    fusion_max_stale_s: float = 120.0

    # --- events -------------------------------------------------------------
    rules_dir: Path = Path("infra/rules")
    dwell_threshold_s: float = 900.0
    speed_limit_kmh: float = 20.0
    anomaly_contamination: float = 0.02
    anomaly_warmup: int = 200

    # --- prediction ---------------------------------------------------------
    forecast_horizon_s: float = 1800.0
    forecast_interval_s: float = 60.0

    # --- alerts -------------------------------------------------------------
    alert_dedup_window_s: float = 120.0
    alert_escalate_after_s: float = 300.0
    alert_webhook_url: str = ""

    # --- llm / copilot ------------------------------------------------------
    ollama_url: str = "http://127.0.0.1:11434"
    llm_model: str = "qwen3:1.7b"
    llm_temperature: float = 0.1
    llm_timeout_s: float = 60.0
    llm_max_tool_steps: int = 6
    openai_base_url: str = ""
    openai_api_key: str = ""
    copilot_allow_degraded: bool = True

    # --- workflow -----------------------------------------------------------
    temporal_host: str = "127.0.0.1:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "sio-playbooks"

    # --- agents -------------------------------------------------------------
    agents_enabled: str = "security,logistics"
    agent_interval_s: float = 30.0
    agent_require_approval: bool = True

    # --- auth ---------------------------------------------------------------
    jwt_secret: str = "dev-only-change-me"
    jwt_issuer: str = "sio-dev"
    jwt_ttl_s: int = 86_400
    keycloak_url: str = "http://127.0.0.1:8080"
    keycloak_realm: str = "sio"
    keycloak_client_id: str = "sio-api"
    opa_url: str = "http://127.0.0.1:8181"
    openfga_url: str = "http://127.0.0.1:8080"
    openfga_store_id: str = ""

    # --- observability ------------------------------------------------------
    metrics_enabled: bool = True
    grafana_port: int = 3000

    # --- retention ----------------------------------------------------------
    retain_frames_days: int = 7
    retain_observations_days: int = 30
    retain_events_days: int = 365
    retain_audit_days: int = 3650

    # --- testing ------------------------------------------------------------
    test_infra: bool = False

    # ------------------------------------------------------------------ derived
    @computed_field  # type: ignore[prop-decorator]
    @property
    def pg_dsn(self) -> str:
        """libpq connection string (psycopg accepts this directly)."""
        return (
            f"postgresql://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_database}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def agent_list(self) -> list[str]:
        return [a.strip() for a in self.agents_enabled.split(",") if a.strip()]

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def run_dir(self) -> Path:
        """Pidfiles and sockets for locally supervised processes."""
        return self.data_dir / "run"

    @property
    def samples_dir(self) -> Path:
        return self.data_dir / "samples"

    def model_path(self, filename: str) -> Path:
        return self.model_dir / filename

    def port_for(self, service: str) -> int:
        """Health/metrics port for ``service``, falling back to 0 (ephemeral) if unknown."""
        value = getattr(self, f"{service.replace('-', '_')}_port", None)
        return int(value) if isinstance(value, int) else 0

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.logs_dir, self.run_dir, self.model_dir, self.samples_dir):
            path.mkdir(parents=True, exist_ok=True)

    def adapter_summary(self) -> dict[str, str]:
        """Which adapter is active per port — surfaced on ``/health`` for debuggability."""
        return {
            "bus": self.bus_backend,
            "graph": self.graph_backend,
            "vector": self.vector_backend,
            "blob": self.blob_backend,
            "detector": self.detector,
            "tracker": self.tracker,
            "embedder": self.embedder,
            "forecaster": self.forecaster,
            "llm": self.llm_provider,
            "auth": self.auth_mode,
            "policy": self.policy_engine,
            "workflow": self.workflow_runner,
            "cep": self.cep_runtime,
        }


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton. Call ``get_settings.cache_clear()`` in tests."""
    return Settings()


def reset_settings() -> None:
    """Drop the cached settings — used by tests that mutate the environment."""
    get_settings.cache_clear()
