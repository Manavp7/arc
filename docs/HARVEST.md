# Harvest log — the pre-git scaffold

An earlier SIO scaffold exists outside version control (on the author's Mac, at
`~/Downloads/arc`, never `git init`'d — which is why this repository looked empty). It is to be
pushed here as a `legacy/scaffold` branch or supplied as a tarball, and this file records what
was taken from it, what was rewritten, and why.

**Rule:** the scaffold is a *draft*, not a contract. Harvest to skip regeneration work; rewrite
the moment it fights the ports design; never let it dictate architecture.

## Status

| | |
|---|---|
| `legacy/scaffold` on `origin` | **not present** as of 2026-07-24 (`git ls-remote` shows only `main`) |
| Consequence | nothing is blocked; every Phase 0 task was specified independently and has been built |
| Action | re-checked at the start of each phase; when it lands, each row below is reconciled and this file updated |

## Inventory reported for the scaffold

~3,300 lines of Python across 15 service packages, a 583-line `libs/sio_schemas`, a 16-file
`web/` that already built, `Justfile`, `mprocs.yaml`, `scripts/bootstrap.sh`, `scripts/seed.py`,
a resolved `uv.lock`, `.env.example`, `.env.gpu.example`, `infra/postgres/init.sql`,
`infra/neo4j/constraints.cypher`, `infra/grafana/provisioning/`, six tests, and
`docs/PRD.md` + `PRD.pdf` + `DEPLOYMENT.md`.

Notably absent from it: **`libs/sio_core`** — no ports layer, no service runtime, no adapter
registry. That absence is the strongest argument for building fresh on a ports-and-adapters
spine rather than patching the old tree: without it, the same consumer loop, health endpoint and
backend branching is duplicated in sixteen services and drifts apart immediately.

## Disposition

| Path in the scaffold | Disposition here | Notes |
|---|---|---|
| `docs/PRD.md` | **reconstructed** | Written from the authoritative text supplied in the build brief, plus Appendix A recording implementation decisions. Removes the only hard dependency on the harvest. |
| `docs/PRD.pdf` | **pending** | Cannot be reconstructed; not needed by any code path. |
| `docs/DEPLOYMENT.md` | **pending** | Superseded in part by `docs/ARCHITECTURE.md` §7 and `docs/MACOS_CHECKLIST.md`; will be reconciled when available. |
| `libs/sio_schemas/` | **rewritten** | Same role, new implementation: alias-based wire names so JSON keeps the PRD's `class`/`from`/`to`, rejection of naive datetimes, ULID-shaped sortable ids, bitemporal `Relationship.holds_at`, and a JSON-Schema exporter with a CI `--check` mode. |
| `Justfile` | **rewritten and extended** | 44 recipes. The scaffold's `neo4j-init` shelled out to `cypher-shell` with a `NEO4J_PASSWORD` default and failed silently on a fresh install — Tier 1 #1 in the flesh; replaced by the detect-then-set logic in `scripts/init_neo4j.py`. |
| `mprocs.yaml` | **rewritten** | Mirrors `scripts/supervisor.py` so the TUI and no-TUI paths cannot diverge. |
| `scripts/bootstrap.sh` | **rewritten** | macOS/Homebrew stays the default branch; Linux is branch-gated and additive. Versions pinned in `scripts/versions.env`. |
| `infra/postgres/init.sql` | **rewritten, split** | Five numbered, checksummed migrations. Adds tenant-leading keys, PostGIS geography + GiST, pgvector HNSW, BRIN on time columns, and trigger-enforced append-only tables. |
| `infra/neo4j/constraints.cypher` | **rewritten** | Composite `(tenant_id, entity_id)` uniqueness plus indexes for the specific traversals the platform issues, including bitemporal edge validity. |
| `infra/grafana/provisioning/` | **pending** | Lands in P6.3 with the analytics service. |
| `.env.example` | **rewritten** | ~120 documented variables, every adapter selector, every governance flag defaulting to the safe position. |
| `.env.gpu.example` | **pending** | Lands in P7.3 with the GPU overlay. |
| `web/` | **rewritten** | The scaffold's build was reported working, but it was not available; the new scaffold is React 19 + Vite 8 + MapLibre 5 + deck.gl 9.3 with a verified `tsc --noEmit` and `vite build`. |
| `tests/` (6 files) | **superseded** | 148 unit tests plus 26 integration tests. The bus contract runs against both adapters; the graph contract runs against Neo4j *and* Postgres. |
| 15 service packages | **reference only** | To be mined for logic during Phases 1–4 and re-homed onto `SioService` + ports. |

### Specific carry-overs already honoured

- `services/perception/.../detector.py` switched on `PERCEPTION_MODE=auto|onnx|synthetic` with a
  `_postprocess` and a `MODEL_PATH` env var. That env contract is kept (as `SIO_DETECTOR` with an
  `auto|onnx|onnx_seg|synthetic|null|deepstream` selector), and `synthetic` is promoted from a
  placeholder to a first-class `Detector` adapter, because a deterministic detector is genuinely
  useful in CI.

## When the branch arrives

1. `git fetch origin && git worktree add .legacy legacy/scaffold` (`.legacy/` is gitignored).
2. Diff each row above; adopt anything better, discard the rest, and update the disposition.
3. Harvest is **one-way**: `.legacy/` → repository, never the reverse. The git remote is the
   single source of truth.
