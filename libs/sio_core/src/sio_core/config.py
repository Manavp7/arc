"""Single source of configuration truth, loaded from the environment and ``.env``.

Every knob in the platform lands here. Services never read ``os.environ`` directly, so
``just doctor`` can report the *effective* configuration and tests can override it with a
plain constructor call.
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# stdlib logging, not `sio_core.telemetry`: telemetry reads settings, so importing it here would be a
# cycle. A config problem must be reportable before logging is configured — that is exactly when it
# happens.
log = logging.getLogger("sio.config")

BusBackend = Literal["redis", "memory", "kafka"]
GraphBackend = Literal["neo4j", "postgres", "memory", "memgraph"]
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
#: `cpu` is the default and what `just check` runs on. `gpu` is the production overlay (PRD §9.3).
Profile = Literal["cpu", "gpu"]

#: What `SIO_PROFILE=gpu` selects, seam by seam.
#:
#: Written as data rather than as a branch so `just doctor` can print it, `docs/GPU_SWAP.md` can be generated
#: against it, and a test can assert every value is a legal member of its own Literal — which is how a typo here
#: becomes a test failure rather than a runtime surprise on someone else's cluster.
GPU_PROFILE: dict[str, str] = {
    # Kafka rather than Redis Streams: at GPU throughput the bottleneck stops being inference and becomes the
    # bus, and Redis Streams has no partitioning story for parallel consumers of one topic.
    "bus_backend": "kafka",
    # Memgraph speaks Bolt and Cypher, so the Neo4j adapter's queries carry over; it is in-memory, which is the
    # point at the write rates a GPU pipeline produces.
    "graph_backend": "memgraph",
    "vector_backend": "qdrant",
    # DeepStream keeps decode, inference and tracking on the GPU — the copy back to host memory per frame is
    # what caps the ONNX path, not the model.
    "detector": "deepstream",
    "tracker": "deepstream",
    "forecaster": "timesfm",
    # Anything OpenAI-compatible: vLLM, NIM, TGI. The adapter names none of them.
    "llm_provider": "openai_compat",
    # A default endpoint, so `SIO_PROFILE=gpu` is COHERENT rather than merely selected. Without this the base
    # URL stays empty, the adapter builds `/v1`, and the profile boots into a configuration that cannot work —
    # which is worse than not having a profile, because it looks configured. 8001 is the port vLLM's own
    # quickstart uses; override it for NIM or a remote host.
    "openai_base_url": "http://127.0.0.1:8001/v1",
}


def _neo4j(name: str) -> AliasChoices:
    """Accept both the conventional ``NEO4J_*`` name and a prefixed ``SIO_NEO4J_*`` name."""
    return AliasChoices(name, f"SIO_{name}")


class Settings(BaseSettings):
    """Effective configuration. See ``.env.example`` for documentation of every field."""

    @model_validator(mode="after")
    def _apply_profile(self) -> Settings:
        """Let `SIO_PROFILE=gpu` set every seam at once — except the ones changed from their default.

        The rule is **"still at its default"**, not `model_fields_set`, and finding out why cost a debugging
        session worth recording: this repository ships a `.env` that explicitly lists EVERY field with its
        default value, as documentation. So `model_fields_set` contains all 130 of them on every run, and a
        profile gated on "was this field supplied?" could never apply anything at all. Pydantic was reporting
        the truth; the truth just was not the question I meant to ask.

        Comparing against the declared default asks the question I actually meant: has somebody *changed* this?
        An operator who edits `SIO_LLM_PROVIDER=scripted` differs from the default and wins — which matters
        because `SIO_PROFILE=gpu SIO_LLM_PROVIDER=scripted` is precisely how you test GPU wiring on a laptop
        with no GPU.

        The honest limitation: setting a seam explicitly TO its default value is indistinguishable from leaving
        it alone, so the profile overrides it. That is the harmless direction, and distinguishing them would
        need provenance tracking that pydantic-settings does not offer.
        """
        if self.profile != "gpu":
            return self
        for field, value in GPU_PROFILE.items():
            declared = type(self).model_fields.get(field)
            if declared is not None and getattr(self, field) == declared.default:
                object.__setattr__(self, field, value)
        return self

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

    # --- the profile ---------------------------------------------------------
    #
    # One flip instead of eleven. Every seam below can be set individually — and for a real deployment that is
    # what you want, because nobody swaps all of them at once. But "run this on GPUs" is a single intention, and
    # making somebody express it as eleven environment variables guarantees that one of them will be missed and
    # the resulting half-swapped system will be blamed on the platform.
    #
    # An explicitly-set seam always WINS over the profile (see `_apply_profile`). A profile that overrode
    # deliberate configuration would be a trap: `SIO_PROFILE=gpu SIO_LLM_PROVIDER=scripted` has to mean what it
    # says, because that combination is exactly how somebody tests GPU wiring without a GPU.
    profile: Profile = "cpu"

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
    webhooks_port: int = 8119
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
    # Phase 1 bridge: the simulator publishes ground-truth entities so the live map has something
    # to show before perception/tracking/fusion exist. Set false once Phase 2 is live.
    sim_publish_entities: bool = True
    sim_entity_hz: float = 2.0
    sim_gps_hz: float = 1.0  # per-tracker GPS reporting rate
    sim_sensor_hz: float = 0.2  # per-sensor IoT reporting rate (every 5 s)
    sim_tick_hz: float = 4.0  # simulation steps per second (motion smoothness)

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
    ort_providers: str = "CPUExecutionProvider"
    """Comma-separated onnxruntime execution providers, in preference order.

    This one string is the entire CPU-to-GPU swap for inference: the same .onnx weights run under
    CUDAExecutionProvider or TensorrtExecutionProvider. Unavailable providers are dropped with a
    warning rather than failing to start.
    """
    perception_fps: float = 2.0
    frame_index_hz: float = 0.5
    """How often each camera's frames are embedded for semantic search.

    Lower than the frame rate on purpose: at 2 fps consecutive frames are near duplicates, so
    embedding every one costs ~60 ms of CPU to add almost no information.
    """
    perception_max_age_s: float = 60.0
    """Skip frames older than this.

    At-least-once delivery plus a consumer group that starts at the beginning of the stream means a
    restart replays history. For a live picture that history is worthless: inferring on a frame from
    an hour ago costs the same as inferring on the current one and tells an operator nothing. The
    timeline still has every observation; only *inference* is skipped.
    """
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

    # --- spatial ------------------------------------------------------------
    h3_resolution: int = 12
    """About 307 m^2 per cell (~19 m across): one cell holds roughly one vehicle, so counts per cell
    mean something. Coarser puts the whole dock apron in one bucket; finer scatters a truck across a
    dozen cells and turns every count into noise."""
    spatial_boundary_margin_m: float = 2.0
    """Hysteresis band on zone boundaries, set from the position uncertainty we expect to trust. An
    entity parked on a boundary reports inside/outside/inside forever without it, and the events table
    would record a truck entering a dock forty times a minute."""
    spatial_enter_confirm_s: float = 2.0
    """How long inside before entry is asserted. Filters a vehicle clipping a corner while turning."""
    spatial_exit_grace_s: float = 15.0
    """How long outside before exit is asserted. Deliberately longer than entry: a false exit closes
    the dwell clock and can fire rules about leaving, so a dropped fix must not look like a departure."""
    spatial_max_silence_s: float = 180.0
    """When nothing has reported an entity for this long, close its memberships. It has not
    necessarily left, but leaving them open forever makes occupancy drift upward permanently."""

    # --- events -------------------------------------------------------------
    rules_dir: Path = Path("infra/rules")
    dwell_threshold_s: float = 900.0
    speed_limit_kmh: float = 20.0
    anomaly_contamination: float = 0.02
    anomaly_warmup: int = 20
    """Samples of history before the detector will judge anything.

    Twenty, not the two hundred a forest wants: features are sampled every 30 s, so 200 meant the
    detector stayed silent for a hundred minutes and was measured doing exactly that — ten samples in,
    all of them reported as "warming". A robust z-score has a usable baseline from a couple of dozen
    points; `PyODDetector` raises this internally because a forest genuinely needs more."""
    anomaly_detector: str = "auto"
    """``auto`` (robust z-score), or ``pyod`` for IsolationForest where it is installed.

    The default is the attributable one: a per-feature robust z-score says *which* measurement was odd,
    which is what an operator can act on. A forest gives a score and needs SHAP or a permutation study
    to explain it."""

    measurement_min_interval_s: float = 15.0
    """Minimum gap between persisted readings for one (source, metric).

    Set from the finest bucket any forecast uses. GPS trackers report at 1 Hz, so without a gate ten
    devices would write 36,000 battery rows an hour to produce buckets identical to those from 15-second
    sampling."""

    # --- prediction ---------------------------------------------------------
    forecast_horizon_s: float = 1800.0
    forecast_interval_s: float = 60.0
    forecast_interval_level: float = 0.9
    """Nominal coverage of the prediction intervals.

    Reported alongside every forecast and *checked* by the backtest endpoint, because an interval level
    nobody verifies is decoration. A 90% interval that contains the truth half the time is not
    conservative, it is wrong."""

    # --- workflow -----------------------------------------------------------
    workflow_dry_run: bool = True
    """Whether playbook steps describe what they would do instead of doing it.

    True by default, deliberately. A workflow engine that can only be exercised by actually closing a gate
    or launching a drone is one nobody exercises, and the demo needs to run a five-step fire response
    without consequences. Every dry-run step records what it WOULD have done, so the run log is still an
    honest account."""

    # --- decision -----------------------------------------------------------
    decision_use_llm: bool = True
    """Whether to ask a model to write the rationale.

    When false — or when the model is unreachable — a template built from the same measurements is used
    instead. That is not a degraded mode: the template quotes the numbers the options were scored on and is
    arguably more trustworthy than a generated paragraph. Either way the model explains the ranking and
    never changes it, because the optimiser is the part that can be checked."""

    # --- alerts -------------------------------------------------------------
    alert_dedup_window_s: float = 120.0
    alert_escalate_after_s: float = 300.0
    alert_webhook_url: str = ""

    # --- llm / copilot ------------------------------------------------------
    ollama_url: str = "http://127.0.0.1:11434"
    llm_model: str = "llama3.2:3b"
    """Pinned by measurement, not by reputation — see `docs/MODELS.md`.

    On this repository's own 25-question fixture over its nine tools, using the prompt the product actually
    ships: 95 % tool selection, 81 % argument accuracy, p95 5.7 s — the best available on tool selection,
    which is the one axis no amount of surrounding code can recover from.

    Its restraint is only 67 %, and that is deliberately not disqualifying: restraint is decided in code
    (`agent.conversational_reply`) rather than delegated, because a greeting is trivially recognisable and
    the best candidate still queried the database to answer "Hello" one time in three.

    An EXACT tag, never `:latest`. A floating tag means the model can change under a deployment without
    anything in the repository changing, and the first symptom is a copilot quietly choosing wrong."""
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
    auth_required: bool = True
    """Whether a principal is required. True by default, and that default matters.

    A governance layer that is off by default is one that ships off. The dev issuer at `/auth/dev/token`
    makes "on" cheap — a token is one POST away — so there is no reason to default to the insecure setting.

    `false` is honoured, for a developer who wants to curl an endpoint without ceremony, and the middleware
    logs a loud warning at startup when it is set so the choice does not outlive its reason.
    """
    oidc_discovery_url: str = "http://127.0.0.1:8080/realms/sio/.well-known/openid-configuration"
    oidc_audience: str = "sio-api"
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

    @field_validator("alert_webhook_url", "openai_base_url", mode="after")
    @classmethod
    def _must_look_like_a_url(cls, value: str) -> str:
        """Treat a setting that is not a URL as unset, loudly.

        This exists because of a specific, cheap, humiliating failure. `.env.example` had:

            SIO_ALERT_WEBHOOK_URL=             # optional outbound webhook

        python-dotenv reads a trailing comment after an EMPTY value as the value, so the webhook URL became
        the literal string `# optional outbound webhook` — and the alerts service dutifully POSTed to it once
        per alert, logging a warning each time. Thousands of them, burying anything real in the log.

        Validating the shape here fixes the whole class rather than the two instances: a typo, a stray
        comment, or a copied Slack URL missing its scheme can no longer become a permanent stream of failed
        requests. Anything that is not empty and does not start with a scheme is discarded and named, because
        silently ignoring a URL somebody thought they had configured is its own kind of bug.
        """
        stripped = value.strip()
        if not stripped:
            return ""
        if stripped.startswith(("http://", "https://")):
            return stripped
        log.warning(
            "config: ignoring %r because it does not begin with http:// or https:// — treated as unset",
            stripped[:60],
        )
        return ""

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.logs_dir, self.run_dir, self.model_dir, self.samples_dir):
            path.mkdir(parents=True, exist_ok=True)

    def adapter_summary(self) -> dict[str, str]:
        """Which adapter is active per port — surfaced on ``/health`` for debuggability."""
        return {
            "profile": self.profile,
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
