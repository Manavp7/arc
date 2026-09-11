# SIO — Spatial Intelligence OS

SIO is a site-operations prototype: ingest observations, maintain a live world model, detect incidents, investigate evidence, and coordinate an approved response. The bundled logistics yard uses simulated data.

The operator console groups work into **Monitor**, **Footage & cases**, **Investigate**, **Respond**, and **Administration**. An alert opens an incident workspace with its location, affected entities, recorded sequence, available camera evidence, response options, and missions in the same zone.

The [recorded review workflow](docs/REVIEW_WORKFLOW.md) connects batch MP4 upload, durable processing jobs, polygon entry/dwell rules, retained analysis, an operator inbox with shift handovers, and cases with bounded evidence packages. [Versioned rule presets](docs/RULE_PRESETS.md) apply across recordings through explicit zone mapping and a reviewed preview. Cases support [multiple immutable evidence sources](docs/CASE_ATTACHMENTS.md), and the [personal notification bell](docs/NOTIFICATIONS.md) tracks assignments, deadlines, analysis and exports in the app. An [evaluation lab](docs/EVALUATION_LAB.md) compares retained runs against frozen human labels and opens [human-observation cases](docs/CASE_ANNOTATIONS.md) from frozen annotations or evaluated misses. [Side-by-side footage review](docs/EVIDENCE_COMPARISON.md) preserves exact analyses and reviewer-declared alignment across attached recordings. Administration includes site planning, measured camera-readiness checks, storage inventory and protected cleanup. A host operator can create and verify a coordinated [database/media backup and isolated restore](docs/REVIEW_BACKUPS.md). [Video review limits](docs/VIDEO_REVIEW.md) describe motion versus ONNX analysis, private originals and interrupted-job recovery.

[Analysis profiles](docs/ANALYSIS_PROFILES.md) select motion or a compatible locally configured ONNX detector, confidence threshold and 1/2 fps sampling; queued runs pin those settings and model hashes through retries. [Private bookmarks](docs/REVIEW_BOOKMARKS.md) retain personal notes against an exact analysis. [Footage navigation](docs/FOOTAGE_NAVIGATION.md) adds keyboard transport, speed controls, approximate protected-playback frame steps and saved sample thumbnails. Recordings remain limited to 180 seconds and 100 MiB. These controls neither install models nor establish live-camera accuracy.

## Current scope

[Recorded object and movement tools](docs/RECORDED_INSIGHTS.md) search local tracks by class, zone, confidence and clip time, open exact saved samples, and calculate finite directional crossings and sampled occupancy. Immutable movement snapshots preserve their configuration and model provenance and export to CSV.

The repository contains 20 Python services, shared schema/runtime/SDK libraries, a React console, a TypeScript SDK, infrastructure migrations, and regression/integration tests. It is an actively developed prototype, not a completed production deployment.

Implemented paths include Redis streaming, PostgreSQL/PostGIS/pgvector storage, tracking/fusion, rules and alerts, forecasting, decision approval, inline playbooks, mission control, historical replay, source configuration/testing, and operational status. Authentication supports explicit development sign-in and a browser OIDC authorization-code flow with PKCE.

The following limits are deliberate and visible:

- The default yard is **simulated**. A connected real source is labelled separately.
- The default copilot is a **scripted router**, and default hash embeddings do **not** provide semantic similarity. Configure and install real models explicitly.
- Workflow actions default to **dry run**. No physical gate/drone command adapter is included. Turning dry run off does not create one.
- Workflow execution is **inline**. Temporal is not implemented; selecting it fails configuration instead of silently using a different runner.
- Kafka, Qdrant, DeepStream, TimesFM, and Cosmos adapters are stubs. The GPU profile is an extension map, not a working GPU deployment.
- Domain services serve one deployment tenant and reject requests from other tenants. API streams, history, and media are tenant-scoped. A general shared multi-tenant production deployment requires further work.
- Source configuration changes, including enable/disable, are persisted and require an ingestion restart. Connection tests read a sample without publishing it.
- Real camera hardware, live identity-provider deployments, production load, and physical effects require deployment-specific validation.

## Local setup

Python 3.12–3.13, Node 22+, `uv`, and `just` are required. On macOS, bootstrap installs PostgreSQL 17 to match current Homebrew PostGIS/pgvector packages. Linux uses PostgreSQL 16 packages. No Docker is required.

```sh
just setup
just dev-lite
```

Open [the console](http://localhost:5173). In development mode, choose a role and sign in explicitly. Operator can investigate and manage missions; commander can approve responses; integrator can configure sources; administrator is available for local development checks.

The default install uses **PostgreSQL + Redis**, the PostgreSQL graph adapter, filesystem blobs, inline workflows, scripted copilot, and hash embeddings. `dev-lite` runs all consumers in one process while retaining their service endpoints. It reduces process overhead; its memory use still depends on the enabled models and workload.

```sh
just doctor                 # dependency, schema, extension and port checks
just services               # start default PostgreSQL + Redis dependencies
just dev                    # full process profile using the same supervisor
just dev-tui                # optional mprocs interface with the same service set
just stop                   # stop the supervised application processes
```

If another PostgreSQL version already occupies port 5432, configure `SIO_PG_PORT` and compatible database credentials. An installed extension package is insufficient when it targets a different server major version. Doctor reports this mismatch; do not repoint an existing database directory at a different PostgreSQL major version.

`just setup-full` installs optional tools. Neo4j, MinIO, Ollama, and Grafana remain optional deployment choices. Preserve existing `.env` choices when changing profiles.

## Real models and sources

```sh
just models                 # download the ONNX model assets (~186 MB)
just models --llm           # additionally pull the configured local LLM; needs running Ollama
```

Select the desired adapters explicitly, for example `SIO_LLM_PROVIDER=ollama` and `SIO_EMBEDDER=clip`, and restart the affected services. Missing dependencies or unavailable models must be resolved before trusting their output.

In **Administration → Sources**, configure a camera or sensor, save it, test its connection, inspect a safe sample, and restart ingestion to apply changes. Camera credentials stay in the server-side source configuration with restrictive file permissions; listings mask secrets. Real camera buffers are private until perception finishes processing/redaction and publishes a ready-frame reference.

See [connector options](docs/CONNECTORS.md), [model details](docs/MODELS.md), and [operations and authentication](docs/OPERATIONS.md).

## Verification

```sh
just check                  # lint, formatting, core types, unit tests, web build, schemas
npm test --prefix web       # SDK transport, state/replay and authentication regressions
just test-infra             # requires configured live datastores
```

The connected acceptance scenario uses real service handlers, rules, PostgreSQL persistence, authentication, approval, and historical replay in a rollback-only database transaction:

```sh
SIO_TEST_INFRA=1 uv run --no-sync pytest tests/integration/test_operator_acceptance.py
```

Its sensor input is authored test data; it does not verify a physical camera, gate, drone, or a live LLM. Other model evaluations include synthetic fixtures and scripted answers. Test definitions and passing builds are not production accuracy measurements.

`just check` requires installed frontend dependencies. Services beyond the shared core are covered by runtime tests; the current mypy gate checks the core/schema libraries.

See the [local verification record](docs/VERIFICATION.md) for executed checks, browser observations and remaining limits.

## Repository map

| Directory | Purpose |
|---|---|
| `web/` | Operator console: map, incident workspace, replay, missions, sources, health |
| `services/` | Ingestion, perception, world model, reasoning, operations, API and integrations |
| `libs/` | Shared Python schemas, runtime, adapters and SDK |
| `sdk/ts/` | Shared browser/TypeScript transport and contracts |
| `infra/` | Database migrations, site geometry, rules, policy and optional infrastructure |
| `tests/` | Unit, integration, scenario and model evaluation tests |
| `scripts/` | Setup, supervision, diagnostics, seeding and verification |
| `docs/` | Product requirements, architecture, integration and operating notes |

The [PRD](docs/PRD.md) describes intended scope and includes future requirements. Older phase notes are historical design material; they are not completion evidence.
