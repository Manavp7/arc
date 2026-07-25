"""Production adapters that this repository cannot honestly verify (PRD §9.3, Phase 7 P7.3).

Some of the GPU-profile seams point at software that needs hardware or infrastructure not available here: a
DeepStream pipeline needs an NVIDIA GPU and the SDK, TimesFM needs a checkpoint and a GPU to be worth using,
Kafka needs a broker. The plan calls for "adapter stubs with documented parity tests", and this file is the
stubs.

**Why a stub rather than a plausible implementation.** I could write a `KafkaBus` against `aiokafka` from the
documentation. It would look complete, it would pass a mocked test, and nobody — including me — would know
whether it worked until somebody pointed it at a real broker in front of a customer. Untested code that looks
finished is worse than absent code, because absent code gets scheduled and finished code gets trusted. The
things I *could* verify in this environment I implemented for real (the OpenAI-compatible adapter, against a mock
speaking the real wire protocol; Memgraph, which speaks Bolt so the existing Neo4j adapter reaches it unchanged).
The things I could not, refuse loudly.

**Why they construct successfully and fail on use.** The GPU profile must *boot*, so `just doctor` can report
the configuration and a deployment can see what it selected before it has the hardware. But an adapter that
accepted work and dropped it would be catastrophic — a bus that acknowledges publishes into nothing is the worst
component in any system. So: construction succeeds, every operation raises, and the message says what to install,
what changes, and what to use instead.
"""

from __future__ import annotations

from typing import Any, NoReturn

from .errors import ConfigError


class NotYetImplementedAdapter:
    """Base for a seam that is wired but not implemented here.

    Every attribute access that is not explicitly defined raises, which means a caller cannot accidentally use a
    partially-implemented adapter: there is no method that silently no-ops, because there are no methods.
    """

    #: What the adapter would be.
    adapter: str = "unknown"
    #: What to install or provide.
    requires: str = ""
    #: What using it changes, so the docs and the error say the same thing.
    changes: str = ""
    #: The seam's setting and the value to fall back to.
    fallback: str = ""

    def __init__(self, **options: Any) -> None:
        # Options are accepted and kept rather than rejected: a deployment configures the endpoint before it has
        # the broker, and refusing the configuration would stop them describing the system they are building.
        self.options = options

    def _refuse(self, operation: str) -> NoReturn:
        raise ConfigError(
            f"{self.adapter} is wired as a seam but not implemented in this repository, so "
            f"{operation!r} cannot run.\n"
            f"  needs:    {self.requires}\n"
            f"  changes:  {self.changes}\n"
            f"  instead:  {self.fallback}\n"
            f"  see:      docs/GPU_SWAP.md\n"
            f"It refuses rather than silently doing nothing, because an adapter that accepts work and "
            f"discards it is the worst possible component in a pipeline."
        )

    def __getattr__(self, name: str) -> Any:
        # Dunder lookups must fall through, or copying, pickling and pytest's own introspection all explode in
        # confusing ways far from the cause.
        if name.startswith("__"):
            raise AttributeError(name)

        def refuse(*_args: Any, **_kwargs: Any) -> NoReturn:
            self._refuse(name)

        return refuse

    async def ping(self) -> bool:
        """Always false, never raising.

        `ping` is the one exception to refusing, because health checks call it on a loop and an exception there
        takes down the endpoint whose job is to report the problem. False plus the description below is the
        honest answer: it is not reachable, and here is why.
        """
        return False

    async def close(self) -> None:
        """A no-op, because closing something never opened must not be an error path."""
        return None

    def describe(self) -> dict[str, str]:
        return {
            "adapter": self.adapter,
            "status": "wired, not implemented here",
            "requires": self.requires,
            "changes": self.changes,
            "fallback": self.fallback,
        }


class KafkaBusStub(NotYetImplementedAdapter):
    """`SIO_BUS_BACKEND=kafka`.

    Why a deployment wants it: at GPU throughput the bottleneck stops being inference and becomes the bus, and
    Redis Streams has no partitioning story for parallel consumers of one topic.

    Why it is not implemented here: the `Bus` port includes `read_range` — replay a time window — which Redis
    Streams gives for free through its time-ordered ids and Kafka does not. Doing it properly needs an offset
    index keyed by time, and getting that wrong produces a replay that silently skips messages. That is not a
    thing to write blind.
    """

    adapter = "KafkaBus"
    requires = "a Kafka cluster and `aiokafka`"
    changes = (
        "partitioned consumer groups, so several perception workers can share one topic; "
        "retention becomes Kafka's rather than a Redis MAXLEN trim; replay needs a time→offset index"
    )
    fallback = "SIO_BUS_BACKEND=redis (the default, and adequate to roughly 50k msg/s here)"


class QdrantVectorStoreStub(NotYetImplementedAdapter):
    """`SIO_VECTOR_BACKEND=qdrant`.

    Why: pgvector shares Postgres's CPU with every other query, and at a few million embeddings the ANN index
    starts to compete with the world model's writes.

    Why not implemented here: the port's `search` filters by tenant, and Qdrant's payload filtering has
    different semantics from a SQL `WHERE` — in particular around what a filtered ANN search does to recall.
    Shipping that untested would mean silently worse search results, which is the hardest kind of bug to notice.
    """

    adapter = "QdrantVectorStore"
    requires = "a Qdrant instance and `qdrant-client`"
    changes = (
        "vector search leaves Postgres, so embedding load stops competing with world-model writes; "
        "tenant isolation moves from a SQL predicate to a Qdrant payload filter"
    )
    fallback = "SIO_VECTOR_BACKEND=pgvector (the default)"


class DeepStreamDetectorStub(NotYetImplementedAdapter):
    """`SIO_DETECTOR=deepstream`.

    Why: the ONNX path's ceiling is not the model, it is the copy back to host memory per frame. DeepStream keeps
    decode, inference and tracking on the GPU, which is worth roughly an order of magnitude on multi-camera
    sites.

    Why not implemented here: it needs an NVIDIA GPU, the DeepStream SDK and a GStreamer plugin chain, none of
    which exist in this environment or on the macOS target. A detector I cannot run once is a detector I should
    not claim.
    """

    adapter = "DeepStreamDetector"
    requires = "an NVIDIA GPU, DeepStream 7.x, and the nvinfer/nvtracker GStreamer plugins"
    changes = (
        "decode, inference and tracking stay on the GPU — the per-frame host copy that caps the ONNX path "
        "disappears; tracking moves into nvtracker, so SIO_TRACKER=deepstream goes with it"
    )
    fallback = "SIO_DETECTOR=onnx (the default `auto` resolves to it on CPU)"


class TimesFmForecasterStub(NotYetImplementedAdapter):
    """`SIO_FORECASTER=timesfm`.

    Why: a pretrained time-series foundation model beats StatsForecast on series with structure it has seen
    before, and needs no per-series fitting.

    Why not implemented here: the checkpoint is a 200MB download and the model is not worth running on CPU, so
    any test I could write here would exercise the plumbing and not the forecaster. The `Forecaster` port is
    narrow, so this is the least risky of the four to add later.
    """

    adapter = "TimesFMForecaster"
    requires = "`timesfm` and a checkpoint; a GPU for it to be worth using"
    changes = (
        "no per-series fitting, and better accuracy on series with familiar structure; "
        "worse on genuinely novel ones, where StatsForecast's explicit seasonality wins"
    )
    fallback = "SIO_FORECASTER=statsforecast (the default)"


class CosmosSimulationStub(NotYetImplementedAdapter):
    """A generative world model behind the simulation service (PRD M11, Tier 3).

    Why: SimPy and Mesa project *counts and positions*. Cosmos would project pixels — a plausible video of what
    the yard looks like in the scenario — which is a different and more persuasive artefact.

    Why not implemented here: it needs multiple datacentre GPUs. This is the seam, documented, so the simulation
    service has somewhere to call when a deployment has the hardware.
    """

    adapter = "CosmosSimulation"
    requires = "NVIDIA Cosmos weights and multi-GPU inference"
    changes = "scenario projections gain rendered video alongside the numeric KPI deltas"
    fallback = "the SimPy/Mesa projection that ships (see services/simulation)"


#: Every stub, by the setting value that selects it.
#:
#: A registry rather than a chain of imports, so `docs/GPU_SWAP.md` and `just doctor` can enumerate what is wired
#: but unimplemented without knowing each name — and so a test can assert every one of them refuses.
STUBS: dict[str, type[NotYetImplementedAdapter]] = {
    "kafka": KafkaBusStub,
    "qdrant": QdrantVectorStoreStub,
    "deepstream": DeepStreamDetectorStub,
    "timesfm": TimesFmForecasterStub,
    "cosmos": CosmosSimulationStub,
}


__all__ = [
    "STUBS",
    "CosmosSimulationStub",
    "DeepStreamDetectorStub",
    "KafkaBusStub",
    "NotYetImplementedAdapter",
    "QdrantVectorStoreStub",
    "TimesFmForecasterStub",
]
