# macOS verification checklist — Phase 0

**Why you are reading this:** SIO is built on an Ubuntu VM but *shipped* for macOS. Phase 0 is
not complete until you confirm this sequence is green on your Mac. Everything below has been
verified on Linux; the point of this page is to catch anything that only works there.

Expect **10–15 minutes**, most of it Homebrew downloads.

---

## 0. Prerequisites

```bash
xcode-select --install          # if you have never installed the CLT
brew --version                  # Homebrew must exist: https://brew.sh
```

Nothing else is required. No Docker. `just`, `uv` and Node are installed by the bootstrap if
missing.

## 1. Clone and bootstrap

```bash
git clone https://github.com/Manavp7/arc.git sio && cd sio
git checkout cursor/bc-1bb37eba-2e4a-44a5-8bf0-c0f8189b477c-5925

bash scripts/bootstrap.sh          # or: just setup   (which also runs steps 2–4)
```

Installs, all pinned in `scripts/versions.env`:

| Formula | Purpose |
|---|---|
| `postgresql@16`, `postgis`, `pgvector` | structured + spatial + vector store |
| `redis` | event bus |
| `neo4j` | world-model graph |
| `minio` | object storage for frames |
| `temporal` | durable workflow playbooks (Phase 4) |
| `uv`, `just`, `node` | toolchain (skipped if present) |

Then `uv sync` and `npm install --prefix web`.

**Expected tail:**

```
==> bootstrap complete
  next:  just services
  then:  just doctor
```

> `--minimal` installs only Postgres and Redis; `--full` adds Grafana, Ollama and GStreamer.

## 2–4. Services and datastore initialisation

```bash
just services      # brew services start postgresql@16, redis, neo4j; minio as a daemon
just db-init       # 5 migrations → 25 tables, PostGIS + pgvector extensions
just neo4j-init    # sets the initial Neo4j password, then applies the graph schema
just minio-init    # creates the sio-media bucket and proves it is writable
```

`just neo4j-init` is the step most likely to be interesting on a fresh Mac. Neo4j Community
ships with `neo4j/neo4j` and refuses queries until the password is changed; the script detects
which of four states your install is in and handles each. Expected output on a first run:

```
neo4j bootstrap: bolt://127.0.0.1:7687 (user: neo4j, database: neo4j)
  server is using the factory credential (neo4j/neo4j); rotating it
  password rotated to the configured value
  applied 12 schema statements
  constraints: 1, indexes: 14, entities: 0
```

If it instead says *"neo4j is running but rejects both the configured password and the factory
default"*, your Homebrew Neo4j already has a password. Either put it in `.env` as
`NEO4J_PASSWORD=`, or reset:

```bash
just services-stop neo4j
uv run python scripts/init_neo4j.py --reset
just services neo4j && just neo4j-init
```

## 5. Doctor — the actual verification

```bash
just doctor
```

**Expected: `healthy: 22 checks passed, N warnings`.** Warnings for Temporal, Ollama and
un-downloaded models are normal at Phase 0. What must be `ok`:

```
  ok    postgres reachable as sio@sio
  ok    extension postgis installed
  ok    extension vector installed
  ok    schema applied (25 tables)
  ok    append-only enforcement active (4 triggers)
  ok    redis reachable at redis://127.0.0.1:6379/0
  ok    graph (neo4j) reachable: 0 entities, 0 edges
  ok    neo4j schema applied (1 constraints, 14 indexes)
  ok    vector store (pgvector) search working, 512-d
  ok    blob store (minio) readable and writable
```

Each of those proves something a listening port does not: extensions actually installed, schema
actually applied, immutability triggers actually present, vector search actually returning a
correct similarity, bucket actually writable.

## 6. The gate

```bash
just check
```

Runs ruff, ruff-format, mypy (strict on both libraries), 148 unit tests, `tsc --noEmit` and
`vite build`. **This must pass with no datastores running at all** — that is the promise of the
infra-free unit ring, and the reason the Linux path cannot silently become a requirement.

Expected: `✓ check passed`, unit tests in roughly 5 seconds.

## 7. Nothing to run yet

```bash
just dev
```

At Phase 0 this correctly reports that no services are built yet and exits immediately. Phase 1
fills in `ingest`, `api` and the live map.

---

## If something fails

```bash
just doctor --report > doctor.txt
```

`--report` appends a paste-able block: OS and architecture, shell and bash version, tool
versions, `brew services list`, `.sio/` size, git commit, active adapters, and live pidfiles.
Send `doctor.txt` plus the failing command's output — that is enough to reproduce nearly
everything.

Useful specifics:

| Symptom | Cause and fix |
|---|---|
| `postgres not listening on 5432` | another Postgres is already running: `brew services list`, or set `SIO_PG_PORT` in `.env` |
| `role "sio" does not exist` | `just services` creates it; if the cluster predates SIO, run `createuser -s sio` |
| `extension postgis missing` | `brew install postgis pgvector` then `just db-init` |
| `bash: declare -A: invalid option` | a bash-4 construct slipped into a script — a bug on my side; `tests/unit/test_scripts_portability.py` should have caught it. Please report. |
| `just: command not found` | `brew install just` |
| `these migrations changed after being applied: 004_reasoning.sql` | Your database predates a change to that file. It is not a code problem and a **fresh** database is unaffected — verified. Run `just db-init --reset` (destructive: it rebuilds the schema). The guard exists because editing an applied migration leaves two databases claiming the same version number with different shapes, which is worse than the inconvenience of this message. |
| Apple silicon vs Intel brew prefix | handled: `pg_bin_dir()` checks `/opt/homebrew` and `/usr/local` |

## Resetting

```bash
just services-stop      # stop everything SIO started
just clean              # delete .sio/ entirely (databases, logs, models, samples)
```

`just clean` is complete on Linux, where every datastore lives under `.sio/`. On macOS the
Homebrew-managed Postgres, Redis and Neo4j keep their data in the Homebrew prefix, so a full
reset there is `just db-init --reset` plus `uv run python scripts/init_neo4j.py --reset`.

---

## Sign-off

Please confirm, or paste the failure:

- [ ] `bash scripts/bootstrap.sh` completed
- [ ] `just services` — postgres, redis, neo4j, minio all listening
- [ ] `just db-init` — 25 tables, both extensions present
- [ ] `just neo4j-init` — password set, 12 statements applied
- [ ] `just minio-init` — bucket created, write probe ok
- [ ] `just doctor` — 22 checks passed
- [ ] `just check` — passed, and passed again with the datastores stopped
