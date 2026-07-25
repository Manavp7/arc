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
just demo       # seeds the yard if needed, runs a scripted incident, narrates what to look at

open http://localhost:5173
```

`just demo` prints a timestamped walkthrough naming which panel to open and when — see
[docs/DEMO.md](docs/DEMO.md) for the five-minute script, including what to say if it is slow.
Run it once before showing anyone: the first question to the copilot loads two gigabytes of model
weights, so a cold answer takes ~17 s and a warm one ~7 s.

```bash
just demo-reset   # clear the working state (keeps all history) so the demo can be re-run
just e2e          # the end-to-end rings against the running platform
```

Low-RAM machine? `just dev-lite` runs every consumer in a single process.
Cleaning up: `just stop` (processes) and `just clean` (all local state under `.sio/`).

---

## What you get

| Layer | Modules |
|---|---|
| **Analytics** | dwell/throughput/utilisation with their distribution *shape* named, an explainable risk index, H3 heatmaps aggregated server-side for privacy, Markdown reports, provisioned Grafana dashboards |
| **What-if** | counterfactual projections seeded from the live world: gate closure, dock breakdown, fire spread with wind, flooding, drone battery, route severance |
| **Copilot & agents** | natural-language interface over the world model, MCP server, autonomous agents with human-on-the-loop approval |
| **Reasoning & action** | event/CEP engine + anomaly detection, forecasting, what-if simulation, OR-Tools decisions, durable Temporal playbooks, alert intelligence |
| **World model** | entity/relationship graph (bitemporal), append-only timeline, embeddings + semantic search |
| **Perception & fusion** | ONNX YOLO26 detection/segmentation, ReID, OCR, ByteTrack tracking, EKF multi-sensor fusion, PostGIS/H3 spatial engine |
| **Data platform** | pluggable connectors, Redis Streams bus, Postgres/PostGIS/pgvector, MinIO object store |
| **Governance** | JWT authn (dev issuer or Keycloak), RBAC+ABAC with generated Rego, PII redaction on by default, append-only audit, enforced multi-tenancy, explanations on every answer |

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
| 4 | Copilot + MCP, workflows, decisions, agents, alerts, the console panels | **done** |
| 4.7 | **Ship checkpoint** — `just demo`, `docs/DEMO.md`, quickstart re-verified, e2e smoke | **done** |
| 5 | Governance enforced: authn/authz, PII redaction, immutable audit, multi-tenancy | **done** |
| 6 | Simulation (M11) and analytics (M19) **done** incl. in-app views; mission control, developer platform | **in progress** |
| 7 | Real connectors (RTSP/STAC/MAVLink/MQTT), 3D twin, GPU/production overlay | — |
| 8 | Evaluation harnesses (mAP/HOTA/copilot), performance benchmarks, docs | — |

## Extending it without changing it

Four extension points, discovered through Python entry points — connectors, rules, copilot tools and agents.
Install a package; the platform picks it up.

```bash
just plugin-demo    # installs examples/plugin_demo: a tide gauge connector + a flood-warning rule
```

Verified end to end: an out-of-tree rule firing on data from an out-of-tree connector, with **no file under
`services/` or `libs/` naming either** — which a test asserts, so "no core changes" is a check rather than a
claim.

[docs/PLUGINS.md](docs/PLUGINS.md) leads with the three conventions that cost me three attempts, each of which
failed *silently*: the rule loaded, reported enabled, and matched nothing.

## Governance

Authentication is required by default, every authorisation decision is audited, personal data is redacted
unless the caller holds both a role and an explicit scope, faces and plates are blurred before any frame
reaches storage, and nothing acts in the physical world without a human approving it.

```bash
# what is actually switched on in a running deployment, including what is NOT
curl -s localhost:8118/governance/posture -H "Authorization: Bearer $TOKEN" | jq
```

[docs/GOVERNANCE.md](docs/GOVERNANCE.md) covers the model, the regulatory posture, and a
**"what is not protected"** section — because a governance document that lists only what is protected is a
marketing document.

Optional providers, both verified against the same test suite:

```bash
just keycloak                                   # then SIO_AUTH_MODE=keycloak just dev
just opa                                        # then SIO_POLICY_ENGINE=opa just dev
```

The Rego is **generated** from the same rule table the embedded engine evaluates (`just policies`), and a
conformance test runs 810 principal × action × context combinations through both engines and asserts they
agree. It found a bug nothing else would have.

## Known limitations

Stated deliberately, because a reader who discovers these for themselves reasonably assumes they were
missed. Each is a choice with a reason.

| | |
|---|---|
| **The yard is simulated.** | Real connectors (RTSP, MQTT, MAVLink, STAC) sit behind the same ports, but the demo drives a physics simulation so the incident is reproducible and the perception path still runs on genuinely rendered frames. |
| **Playbook steps are dry-run by default.** | They record what they *would* have done. A workflow engine that can only be exercised by actually closing a gate is one nobody exercises. `SIO_WORKFLOW_DRY_RUN=false` to arm it. |
| **Fire detection is a colour-and-motion heuristic.** | It runs on the real rendered camera frames, so the detection is genuine; the detector is simple. A trained model drops in behind the same port. |
| **The copilot is a 3 B local model.** | 95 % tool selection and 81 % argument accuracy on this repo's own 25-question fixture (`docs/MODELS.md` has the numbers and the four models that lost). Restraint — not calling a tool for "hello" — is handled in code, because the best candidate still queried the database to answer a greeting one in three times. |
| **Keycloak and OPA are optional.** | The dev default is a signed local JWT and a permissive policy, both tested. Production wiring is documented, not demonstrated. Phase 5 enforces it. |
| **Single tenant in the demo.** | Every table and query is tenant-scoped; the demo runs one. |
| **Forecast intervals can be too wide to act on.** | The prediction service says so rather than narrowing them, which would be inventing confidence. A summary reading "effectively the whole range" is the system being honest, not broken. |
| **No list virtualisation in the console.** | The event feed and alert inbox render a capped window and state the total. Fine at demo scale, a real cost at ten thousand rows. |
| **macOS is the supported target.** | Linux (Ubuntu 24.04+) is verified in CI. Windows is not addressed. |

## Licence

Apache-2.0. Model licences are tracked per-model in [`docs/MODELS.md`](docs/MODELS.md).
