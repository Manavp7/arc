# The GPU / production swap

```bash
SIO_PROFILE=gpu just services
```

One flag flips every seam. This document says exactly what it changes, what hardware each piece needs, and —
importantly — **which of them this repository has actually verified**.

## What the profile selects

| seam | cpu (default) | gpu | verified here |
|---|---|---|---|
| `bus_backend` | `redis` | `kafka` | ✗ stub |
| `graph_backend` | `neo4j` | `memgraph` | ✓ **real** |
| `vector_backend` | `pgvector` | `qdrant` | ✗ stub |
| `detector` | `auto` → onnx | `deepstream` | ✗ stub |
| `tracker` | `bytetrack` | `deepstream` | ✗ stub |
| `forecaster` | `statsforecast` | `timesfm` | ✗ stub |
| `llm_provider` | `ollama` | `openai_compat` | ✓ **real, tested against a mock server** |
| `openai_base_url` | — | `http://127.0.0.1:8001/v1` | ✓ |

`SIO_PROFILE=gpu` **boots**. Every seam resolves to something, `just doctor` can report the configuration before
the hardware arrives, and `/health` carries `profile: gpu` so a swap is visible in production rather than
inferred from behaviour.

## An explicitly-changed seam survives the profile

```bash
SIO_PROFILE=gpu SIO_LLM_PROVIDER=scripted just services   # GPU wiring, no GPU
```

The rule is **"still at its declared default"**. A seam you have changed is respected; one you have not takes the
profile.

That rule is not the obvious one, and the obvious one does not work. The first version used pydantic's
`model_fields_set` to mean "the operator set this deliberately" — and applied nothing at all, because this
repository ships a `.env` listing *every* field with its default value as documentation, so `model_fields_set`
contains all ~130 fields on every run. Pydantic was reporting the truth; the truth was not the question.

The honest limitation: setting a seam explicitly **to** its default value is indistinguishable from leaving it
alone, so the profile overrides it. That is the harmless direction, and telling them apart would need provenance
tracking pydantic-settings does not offer.

---

## What is real

### `openai_compat` — vLLM, NIM, TGI

The production LLM seam, and the one that is properly tested. Everything serving a large model behind HTTP has
converged on `/v1/chat/completions`, so one adapter reaches Nemotron 3 on vLLM, a NIM container and TGI without
naming any of them.

```bash
# vLLM
python -m vllm.entrypoints.openai.api_server \
  --model nvidia/nemotron-3-8b-chat --port 8001

SIO_PROFILE=gpu \
SIO_OPENAI_BASE_URL=http://127.0.0.1:8001/v1 \
SIO_LLM_MODEL=nvidia/nemotron-3-8b-chat \
just services
```

**Hardware:** one 24GB GPU for an 8B model at fp16; 2×80GB for a 70B. Add `SIO_OPENAI_API_KEY` for NIM.

There is a mock OpenAI-compatible server in the test suite and the same assertions run against it as against
Ollama. The case that justifies the exercise:

> OpenAI-compatible servers send `tool_calls[].function.arguments` as a JSON **string**, by specification.
> Ollama sends an **object**.

Both route through the shared `parse_tool_calls`, which already repairs the string form because small local
models produce it by accident. A second extractor in the new adapter would have been a live bug on flip day, and
it would have presented as *a copilot that answers fluently and never calls a tool* — which reads like a model
quality problem, not a wiring one.

Three more differences the tests pin:

* **Some servers cannot call tools.** llama.cpp ignores the parameter; TGI returns 422. Receiving prose where a
  tool call was required is worse than being told no, so a refusal is detected, retried once without tools, and
  **remembered** — it is a fact about the deployment, not the request.
* **The failure mode of a shared endpoint is a queue, not an error.** A busy vLLM accepts and holds. The timeout
  is 120s: a 30s one produces a copilot that fails under exactly the load it was bought for.
* **A 200 with no choices** means a draining backend, not an empty answer.

### `memgraph`

The only free seam in the profile. Memgraph speaks Bolt and Cypher, so the existing Neo4j adapter reaches it
**unchanged** — one line in the registry.

```bash
docker run -p 7687:7687 memgraph/memgraph
SIO_PROFILE=gpu SIO_NEO4J_URI=bolt://127.0.0.1:7687 just services
```

**What differs is operational, not protocol:** Memgraph is in-memory, which is the point at the write rates a
GPU pipeline produces, and its durability is snapshots rather than a WAL. Size RAM for the whole graph.

---

## What is stubbed, and why

Each of these **constructs successfully** — so the profile boots and can be inspected — and **refuses every
operation** with a message naming what to install, what changes, and what to use instead:

```
KafkaBus is wired as a seam but not implemented in this repository, so 'publish' cannot run.
  needs:    a Kafka cluster and `aiokafka`
  changes:  partitioned consumer groups, so several perception workers can share one topic;
            retention becomes Kafka's rather than a Redis MAXLEN trim; replay needs a time→offset index
  instead:  SIO_BUS_BACKEND=redis (the default, and adequate to roughly 50k msg/s here)
  see:      docs/GPU_SWAP.md
```

**Why a stub rather than a plausible implementation.** I could write a `KafkaBus` against `aiokafka` from the
documentation. It would look complete, pass a mocked test, and nobody — including me — would know whether it
worked until it met a real broker in front of a customer. Untested code that looks finished is worse than absent
code, because absent code gets scheduled and finished code gets trusted.

**Why they refuse rather than no-op.** A bus that acknowledges publishes into nothing is the worst possible
component in a pipeline: every counter healthy, no data anywhere. `ping()` is the single exception — it returns
`False` without raising, because health checks call it on a loop and an exception there takes down the endpoint
whose job is to report the problem.

### `kafka`

**Wants:** at GPU throughput the bottleneck stops being inference and becomes the bus; Redis Streams has no
partitioning story for parallel consumers of one topic.

**Blocked on:** the `Bus` port includes `read_range` — replay a time window — which Redis Streams gives free
through time-ordered ids and Kafka does not. Doing it properly needs an offset index keyed by time, and getting
that wrong produces a replay that *silently skips messages*.

**Hardware:** 3 brokers for production; `redpanda` is a single-binary substitute for development.

### `qdrant`

**Wants:** pgvector shares Postgres's CPU with every other query, and at a few million embeddings the ANN index
competes with world-model writes.

**Blocked on:** the port's `search` filters by tenant, and Qdrant's payload filtering has different recall
characteristics from a SQL `WHERE`. Shipping that untested means silently worse search results — the hardest kind
of bug to notice.

### `deepstream`

**Wants:** the ONNX path's ceiling is not the model, it is the per-frame copy back to host memory. DeepStream
keeps decode, inference and tracking on the GPU — roughly an order of magnitude on multi-camera sites.

**Blocked on:** an NVIDIA GPU, DeepStream 7.x and an nvinfer/nvtracker GStreamer chain, none of which exist here
or on the macOS target. A detector I cannot run once is a detector I should not claim.

**Note:** `SIO_TRACKER=deepstream` goes with it — tracking moves into nvtracker, so ByteTrack is bypassed rather
than fed.

### `timesfm`

**Wants:** a pretrained time-series foundation model, no per-series fitting, better on series with familiar
structure.

**Blocked on:** a 200MB checkpoint that is not worth running on CPU, so any test written here would exercise
plumbing rather than the forecaster. The `Forecaster` port is narrow, making this the least risky of the four to
add later.

**Trade:** worse than StatsForecast on genuinely novel series, where explicit seasonality wins.

### `cosmos`

**Wants:** SimPy and Mesa project counts and positions; Cosmos would project *pixels* — a plausible video of the
scenario, which is a different and more persuasive artefact.

**Blocked on:** multiple datacentre GPUs.

---

## Flink / Bytewax CEP

`SIO_CEP_RUNTIME` accepts `native` (the default) and `bytewax`. The native engine evaluates rules in-process,
which is right up to the point where rule evaluation itself needs to scale horizontally.

Flink is the usual answer at that scale and is **not** wired, deliberately: it would mean rules living in a
second runtime with its own deployment, and the rule DSL this platform ships — including the no-code builder —
would need a compiler targeting it. That is a project, not a seam. Bytewax is the pragmatic middle: same Python,
distributable, and the rules could be lifted with modest changes.

---

## Checking a swap

```bash
just doctor                     # what each seam resolved to
curl -s localhost:8000/health   # `profile` and the adapter summary
SIO_PROFILE=gpu just test       # the suite, at the seams
```

The last one is the plan's acceptance and it passes: the same tests run under both profiles, because what they
exercise is the seam rather than the hardware.

What that does **not** prove, and pretending otherwise would make this document worse than useless: it does not
show that Nemotron answers well, that a GPU is fast, or that DeepStream links against your CUDA. It shows the
seam holds — same reply shape, same tool extraction, same repair behaviour, same failure reporting — which is
the part this repository is responsible for.
