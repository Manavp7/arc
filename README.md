# SIO — Spatial Intelligence OS

An AI operating system for understanding, tracking, predicting and orchestrating the
physical world. SIO ingests heterogeneous real-world signals (cameras, GPS, IoT, drones,
satellite, public APIs), fuses them into a single live world model (a spatiotemporal
knowledge graph), reasons over it (events, prediction, simulation, decisions, agents), and
exposes it through a natural-language copilot, autonomous agents, durable workflows and a
live digital twin.

One reusable platform, not many vertical products. Smart city, ports, warehouses, disaster
response, industrial safety — each is a *configuration* of the same substrate.

> Full product definition: [`docs/PRD.md`](docs/PRD.md).
> Architecture and the swappable-seam map: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

**Status:** Phase 3 complete. Real ONNX detection feeds tracking and multi-sensor fusion; zone
membership fires events through a declarative rule engine; forecasts carry intervals whose coverage has
been measured against held-out history; and the console scrubs and replays the recorded past.
See [Roadmap](#roadmap).

---

## Quickstart

Primary supported platform is **macOS (Apple Silicon) via Homebrew, no Docker**.
Linux (Ubuntu 24.04+) is additionally supported for CI and verification.

```bash
git clone https://github.com/Manavp7/arc.git sio && cd sio

just setup      # brew/apt dependencies, uv sync, npm install, .env, db + graph + bucket init
just doctor     # verify every dependency, port and datastore — read this if anything fails
just services   # postgres, redis, neo4j, minio (+ temporal, grafana, ollama)
just models     # ~45 MB of ONNX weights (detection, segmentation, ReID, CLIP), checksummed
just dev        # all services + web, in an mprocs TUI
just demo       # seed the yard and run the scripted incident

open http://localhost:5173
```

Low-RAM machine? `just dev-lite` runs every consumer in a single process.
Cleaning up: `just stop` (processes) and `just clean` (all local state under `.sio/`).

---

## What you get

| Layer | Modules |
|---|---|
| **Copilot & agents** | natural-language interface over the world model, MCP server, autonomous agents with human-on-the-loop approval |
| **Reasoning & action** | event/CEP engine + anomaly detection, forecasting, what-if simulation, OR-Tools decisions, durable Temporal playbooks, alert intelligence |
| **World model** | entity/relationship graph (bitemporal), append-only timeline, embeddings + semantic search |
| **Perception & fusion** | ONNX YOLO26 detection/segmentation, ReID, OCR, ByteTrack tracking, EKF multi-sensor fusion, PostGIS/H3 spatial engine |
| **Data platform** | pluggable connectors, Redis Streams bus, Postgres/PostGIS/pgvector, MinIO object store |
| **Governance** | authn/authz, PII redaction, immutable audit, lineage, multi-tenancy, explanations on every answer |

Everything is explainable by default: copilot answers, events, alerts and decisions all
carry an evidence chain (sources, confidence, timeline, related entities, alternatives).

---

## Design rules

1. **Platform over product.** Every feature is a reusable primitive.
2. **Explainable by default.** No black-box decisions.
3. **Real-time first.** A streaming nervous system, not batch jobs.
4. **Swappable seams.** Every external engine sits behind a port with at least two
   adapters, so the CPU-first local stack becomes a GPU/production stack by changing
   environment variables — never code. See [`docs/GPU_SWAP.md`](docs/GPU_SWAP.md).
5. **Governance as runtime.** Privacy, RBAC/ABAC, audit and lineage are enforced in the
   pipeline, not documented in a wiki.
6. **Infra-free unit tests.** `just check` runs on a laptop with nothing installed but
   Python and Node.

### CPU-first, and genuinely fast

The entire perception stack is **ONNX Runtime only — no PyTorch, no PaddlePaddle, no
TensorFlow**. YOLO26-nano runs detection in ~25 ms/frame on a laptop CPU, and the whole
model set (detection + segmentation + ReID + CLIP) is about 45 MB. The GPU swap is an
execution-provider string, not a dependency matrix.

---

## Repository map

```
libs/sio_schemas   versioned data contracts (observation → detection → track → entity → …)
libs/sio_core      ports & adapters, service runtime, telemetry, explanations
libs/sio_sdk       Python SDK
services/*         one uv project per service, each a bus consumer/producer with /health
web/               React + Vite + MapLibre + deck.gl live map, timeline, copilot, alerts
infra/             Postgres SQL, Neo4j constraints, Grafana dashboards, rules, policies, site
scripts/           bootstrap, doctor, service control, seeding, model fetch, demo
tests/             unit (no infra) · integration (live infra) · e2e (scenarios) · eval
docs/              PRD, architecture, deployment, governance, GPU swap, models, demo
```

## Roadmap

| Phase | Content | State |
|---|---|---|
| 0 | Foundations: workspace, schemas, core ports, bootstrap, datastores, Justfile, CI | **done** (macOS sign-off pending) |
| 1 | Skeleton that boots: ingest simulator → api → live map | **done** |
| 2 | Perception → tracking → fusion → world model + semantic search | **done** |
| 3 | Spatial engine, events + anomalies, forecasting, timeline replay | **done** |
| 4 | Copilot + MCP, workflows, decisions, agents, alerts → **demo ships here** | — |
| 5 | Governance enforced: authn/authz, PII redaction, audit, multi-tenancy | — |
| 6 | Simulation, mission control, analytics, developer platform (SDKs/webhooks/plugins) | — |
| 7 | Real connectors (RTSP/STAC/MAVLink/MQTT), 3D twin, GPU/production overlay | — |
| 8 | Evaluation harnesses (mAP/HOTA/copilot), performance benchmarks, docs | — |

## Licence

Apache-2.0. Model licences are tracked per-model in [`docs/MODELS.md`](docs/MODELS.md).
