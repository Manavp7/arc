# SIO — Spatial Intelligence OS
#
# `just` with no arguments lists every recipe. Start with:
#   just setup     install dependencies and initialise the datastores
#   just doctor    verify the environment (read this first when something is wrong)
#   just dev       run the platform
#
# macOS is the supported platform (Homebrew, no Docker); Linux works for CI and verification.

set shell := ["bash", "-uc"]
set dotenv-load := true
set dotenv-filename := ".env"
set positional-arguments := true

# `uv run` is used everywhere rather than activating a virtualenv, so recipes behave the same
# in a shell, in CI, and under a process supervisor.
uv := "uv run --no-sync"

_default:
    @just --list --unsorted

# ---------------------------------------------------------------------------- setup
# Install everything and initialise the datastores. Safe to re-run.
setup:
    bash scripts/bootstrap.sh
    just services
    just db-init
    just neo4j-init
    just minio-init
    just doctor

# Install dependencies only (no datastores) — useful in CI.
setup-deps:
    bash scripts/bootstrap.sh --deps

# Minimal install: postgres + redis only.
setup-minimal:
    bash scripts/bootstrap.sh --minimal

# Everything, including grafana, ollama and gstreamer.
setup-full:
    bash scripts/bootstrap.sh --full

# Verify the environment. Add --report for a paste-able diagnostic block.
doctor *args:
    bash scripts/doctor.sh {{args}}

# ----------------------------------------------------------------- infrastructure
# Start postgres, redis, neo4j and minio (add names to be selective, or "all").
services *args:
    bash scripts/services.sh start {{args}}

services-stop *args:
    bash scripts/services.sh stop {{args}}

services-status:
    bash scripts/services.sh status

services-restart *args:
    bash scripts/services.sh restart {{args}}

# Apply the Postgres schema (idempotent; --check to report, --reset to rebuild).
db-init *args:
    {{uv}} python scripts/init_db.py {{args}}

# Set Neo4j's initial password and apply the graph schema (idempotent).
neo4j-init *args:
    {{uv}} python scripts/init_neo4j.py {{args}}

# Create the MinIO media bucket (idempotent).
minio-init *args:
    {{uv}} python scripts/init_minio.py {{args}}

# Open a psql shell against the SIO database.
psql:
    #!/usr/bin/env bash
    set -euo pipefail
    . scripts/lib.sh
    PGPASSWORD="${SIO_PG_PASSWORD:-sio}" "$(pg_bin_dir)/psql" \
        -h "${SIO_PG_HOST:-127.0.0.1}" -p "${SIO_PG_PORT:-5432}" \
        -U "${SIO_PG_USER:-sio}" -d "${SIO_PG_DATABASE:-sio}"

# Open a cypher-shell against Neo4j.
cypher:
    #!/usr/bin/env bash
    set -euo pipefail
    . scripts/lib.sh
    "${SIO_STATE_DIR}/neo4j/bin/cypher-shell" -a "${NEO4J_URI:-bolt://127.0.0.1:7687}" \
        -u "${NEO4J_USER:-neo4j}" -p "${NEO4J_PASSWORD:-siolocalpassword}" 2>/dev/null \
        || cypher-shell -a "${NEO4J_URI:-bolt://127.0.0.1:7687}" \
           -u "${NEO4J_USER:-neo4j}" -p "${NEO4J_PASSWORD:-siolocalpassword}"

# Tail every infrastructure log.
logs:
    tail -n 40 -f .sio/logs/*.log

# --------------------------------------------------------------------------- models
# Download the ONNX model set (~45 MB: detection, segmentation, ReID, CLIP) and pull the LLM.
models *args:
    {{uv}} python scripts/fetch_models.py {{args}}

# Build the deterministic sample clips used by perception tests and the demo.
samples *args:
    {{uv}} python scripts/make_sample_clip.py {{args}}

# ------------------------------------------------------------------------------ run
# Run the whole platform (mprocs if available, otherwise the built-in supervisor).
dev *args:
    #!/usr/bin/env bash
    set -euo pipefail
    if command -v mprocs >/dev/null 2>&1 && [ -f mprocs.yaml ]; then
        mprocs --config mprocs.yaml
    else
        {{uv}} python scripts/supervisor.py --profile full {{args}}
    fi

# Run every consumer in a single process — for low-RAM machines.
dev-lite *args:
    {{uv}} python scripts/supervisor.py --profile lite {{args}}

# Run only the data path (ingest, api, web).
dev-core *args:
    {{uv}} python scripts/supervisor.py --profile core {{args}}

# Stop anything `just dev` started (uses pidfiles, never pkill).
stop:
    {{uv}} python scripts/supervisor.py --stop

api:
    {{uv}} python -m sio_api

web:
    cd web && npm run dev

# Seed the simulated site and start generating signals.
seed *args:
    {{uv}} python scripts/seed.py {{args}}

# Run the scripted demo incident end to end and print a narrated walkthrough.
demo *args:
    {{uv}} python scripts/demo.py {{args}}

demo-reset:
    {{uv}} python scripts/demo.py --reset

# ---------------------------------------------------------------------------- checks
# The gate every phase must pass: lint, format, types, unit tests, web build.
check: lint typecheck test web-check
    @echo "✓ check passed"

# The end-to-end rings. Needs a running platform — `just services && just dev` first.
#
# Separate from `just check` on purpose: `check` must pass on a laptop with nothing running, which is what
# makes it usable as a pre-commit gate. These need fifteen processes and two minutes.
e2e:
    @echo "running the end-to-end rings against the live platform..."
    SIO_TEST_INFRA=1 {{uv}} pytest tests/e2e tests/integration -v

lint:
    {{uv}} ruff check .
    {{uv}} ruff format --check .

fmt:
    {{uv}} ruff check --fix .
    {{uv}} ruff format .

typecheck:
    {{uv}} mypy libs/sio_schemas/src libs/sio_core/src

# Unit tests only: no infrastructure required, runs anywhere.
test *args:
    {{uv}} pytest tests/unit {{args}}

# Integration tests against live datastores.
test-infra *args:
    SIO_TEST_INFRA=1 {{uv}} pytest tests/integration -m infra {{args}}

# End-to-end scenario tests (fire playbook, dwell query, replay).
test-e2e *args:
    SIO_TEST_INFRA=1 {{uv}} pytest tests/e2e -m e2e {{args}}

test-all: check
    SIO_TEST_INFRA=1 {{uv}} pytest tests

# Quality harnesses: detection mAP, tracking HOTA, copilot Q&A, event precision/recall.
eval *args:
    {{uv}} pytest tests/eval -m eval {{args}}

# Verify the copilot's model can actually select tools (gate for Phase 4).
eval-tools *args:
    {{uv}} python scripts/eval_tool_calling.py {{args}}

# Regenerate the JSON Schema exports from the pydantic contracts.
schemas:
    {{uv}} python -m sio_schemas.export --out docs/schemas

web-check:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -d web/node_modules ]; then
        cd web && npx tsc --noEmit && npx vite build
    else
        echo "web/node_modules missing — skipping web check (run: just setup)"
    fi

# --------------------------------------------------------------------------- utility
# Remove all local state: databases, logs, models, sample media. Destructive but complete.
clean:
    #!/usr/bin/env bash
    set -euo pipefail
    just services-stop || true
    rm -rf .sio
    echo "removed .sio (databases, logs, models, samples)"

# Remove build artefacts and caches, keeping datastores intact.
clean-build:
    rm -rf .venv web/dist web/node_modules .pytest_cache .mypy_cache .ruff_cache
    find . -name __pycache__ -type d -prune -exec rm -rf {} +

# Show the effective configuration, including which adapter is active per port.
config:
    {{uv}} python -c "from sio_core.config import get_settings; import json; \
        cfg = get_settings(); print(json.dumps(cfg.adapter_summary(), indent=2)); \
        print('postgres:', cfg.pg_host + ':' + str(cfg.pg_port) + '/' + cfg.pg_database); \
        print('redis:   ', cfg.redis_url); print('neo4j:   ', cfg.neo4j_uri); \
        print('minio:   ', cfg.minio_endpoint + '/' + cfg.minio_bucket)"

# Regenerate the OPA policy from the POLICY table in sio_core.authz.
#
# Generated rather than hand-written, because two implementations of one authorisation policy drift — and
# the drift is a permissions difference between environments. `test_authz.py` asserts the checked-in file
# matches, so a rule added in Python and not regenerated fails CI rather than diverging quietly.
policies:
    @mkdir -p infra/opa/policies
    {{uv}} python -c "from sio_core.authz import rego_from_policy; from pathlib import Path; Path('infra/opa/policies/sio.rego').write_text(rego_from_policy()); print('wrote infra/opa/policies/sio.rego')"

# Start Keycloak and import the SIO realm (optional; the default dev issuer needs nothing).
keycloak:
    bash scripts/keycloak_bootstrap.sh

# Start OPA with the generated policy (optional; the embedded engine evaluates the same rules).
opa: policies
    bash scripts/opa_bootstrap.sh

# Start Grafana with the provisioned SIO datasources and dashboards (optional).
grafana:
    bash scripts/grafana_bootstrap.sh

# Install the example out-of-tree plugin, and prove it appears at runtime.
#
# `--no-deps` because sio-core and sio-schemas are already in this environment. The example deliberately has no
# `[tool.uv.sources]` workspace refs: a plugin that only builds inside the repository it extends proves nothing
# Run the SDK quickstart against a running platform (docs/SDK.md)
sdk-demo:
    @uv run python examples/sdk_quickstart.py

# about extensibility, so it declares plain dependencies exactly as a third party's package would.
plugin-demo:
    uv pip install -e examples/plugin_demo --no-deps
    @echo
    @echo "installed. what the platform now sees:"
    {{uv}} python -c "from sio_core.plugins import discover_all; [print(f'  {g}: {sorted(r.loaded)}') for g, r in discover_all().items() if r.loaded]"
    @echo
    @echo "run the tests that prove it: uv run pytest tests/unit/test_plugins.py -v"

# Remove it again, to check the platform runs without it.
plugin-demo-remove:
    uv pip uninstall sio-plugin-demo
