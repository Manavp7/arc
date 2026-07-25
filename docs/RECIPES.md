# Every `just` recipe

Generated from the `Justfile` by `just recipes`, and **checked by a test**:
`tests/unit/test_docs.py` fails if a recipe is added without appearing here.

A reference maintained by hand is wrong within a month, and a wrong reference is worse than none —
it sends somebody to a command that does not exist and they conclude the docs cannot be trusted.
The descriptions come from the comments above each recipe, so `just --list` and this page cannot
disagree.

There are **53**.

## Setup

| recipe | what it does |
|---|---|
| `just setup` | Install everything and initialise the datastores. Safe to re-run. |
| `just setup-deps` | Install dependencies only (no datastores) — useful in CI. |
| `just setup-minimal` | Minimal install: postgres + redis only. |
| `just setup-full` | Everything, including grafana, ollama and gstreamer. |
| `just doctor *args` | Verify the environment. Add --report for a paste-able diagnostic block. |

## Infrastructure

| recipe | what it does |
|---|---|
| `just services *args` | Start postgres, redis, neo4j and minio (add names to be selective, or "all"). |
| `just services-stop *args` | Stop the datastores, keeping their data. |
| `just services-status` | What is running, on which port, and whether it answers. |
| `just services-restart *args` | Restart the datastores, keeping their data. |
| `just db-init *args` | Apply the Postgres schema (idempotent; --check to report, --reset to rebuild). |
| `just neo4j-init *args` | Set Neo4j's initial password and apply the graph schema (idempotent). |
| `just minio-init *args` | Create the MinIO media bucket (idempotent). |
| `just psql` | Open a psql shell against the SIO database. |
| `just cypher` | Open a cypher-shell against Neo4j. |
| `just logs` | Tail every infrastructure log. |

## Models

| recipe | what it does |
|---|---|
| `just models *args` | Download the ONNX model set (~45 MB: detection, segmentation, ReID, CLIP) and pull the LLM. |
| `just samples *args` | Build the deterministic sample clips used by perception tests and the demo. |

## Run

| recipe | what it does |
|---|---|
| `just dev *args` | Run the whole platform (mprocs if available, otherwise the built-in supervisor). |
| `just dev-lite *args` | Run every consumer in a single process — for low-RAM machines. |
| `just dev-core *args` | Run only the data path (ingest, api, web). |
| `just stop` | Stop anything `just dev` started (uses pidfiles, never pkill). |
| `just api` | Run the API gateway alone (the rest of the stack must already be up). |
| `just web` | Run the web console's dev server alone. |
| `just seed *args` | Seed the simulated site and start generating signals. |
| `just demo *args` | Run the scripted demo incident end to end and print a narrated walkthrough. |
| `just demo-reset` | Wipe the demo's state and start the scenario again from zero. |

## Checks

| recipe | what it does |
|---|---|
| `just check` | The gate every phase must pass: lint, format, types, unit tests, web build. |
| `just e2e` | makes it usable as a pre-commit gate. These need fifteen processes and two minutes. |
| `just lint` | Lint and check formatting without changing anything. |
| `just fmt` | Format every Python and web source in place. |
| `just typecheck` | Run mypy over the libraries and services. |
| `just test *args` | Unit tests only: no infrastructure required, runs anywhere. |
| `just test-infra *args` | Integration tests against live datastores. |
| `just test-e2e *args` | End-to-end scenario tests (fire playbook, dwell query, replay). |
| `just test-all` | Every ring: unit, integration and e2e. Needs infrastructure. |
| `just eval *args` | Quality harnesses: detection mAP, tracking HOTA, copilot Q&A, event precision/recall. |
| `just recipes` | Regenerate docs/RECIPES.md from this file. A test fails if it is stale. |
| `just bench *args` | Needs a running platform. |
| `just eval-tools *args` | Verify the copilot's model can actually select tools (gate for Phase 4). |
| `just schemas` | Regenerate the JSON Schema exports from the pydantic contracts. |
| `just web-check` | Typecheck and build the web console. |

## Utility

| recipe | what it does |
|---|---|
| `just clean` | Remove all local state: databases, logs, models, sample media. Destructive but complete. |
| `just clean-build` | Remove build artefacts and caches, keeping datastores intact. |
| `just config` | Show the effective configuration, including which adapter is active per port. |
| `just policies` | matches, so a rule added in Python and not regenerated fails CI rather than diverging quietly. |
| `just keycloak` | Start Keycloak and import the SIO realm (optional; the default dev issuer needs nothing). |
| `just opa` | Start OPA with the generated policy (optional; the embedded engine evaluates the same rules). |
| `just grafana` | Start Grafana with the provisioned SIO datasources and dashboards (optional). |
| `just sdk-ts` | Regenerate the TypeScript SDK types from the running API, then typecheck |
| `just sdk-ts-demo` | Run the TypeScript SDK quickstart against a running platform |
| `just sdk-demo` | Run the SDK quickstart against a running platform (docs/SDK.md) |
| `just plugin-demo` | about extensibility, so it declares plain dependencies exactly as a third party's package would. |
| `just plugin-demo-remove` | Remove it again, to check the platform runs without it. |
