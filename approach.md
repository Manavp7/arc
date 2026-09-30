# Investigation and operations upgrade

## 2026-09-23 (Asia/Kolkata) — authorized implementation

Objective: implement the five proposed improvements: exact investigation links and resume, recorded visual search, a shared recording timeline, durable outbound alert delivery, and reviewed source activation with rollback.

Boundaries: preserve existing recordings, cases, models, PostgreSQL/Redis data, backups, authentication and source credentials. Use existing UI conventions and storage. No external messages, live-camera activation, production deployment or GitHub publication is part of local verification. Real hardware and external destination verification require their actual configuration.

### Dependencies and ownership

1. Coordinator: restore locked development dependencies; own shared API route registration, shared auth mappings, App navigation, recording clock/timeline work, final integration and this document.
2. Recorded search worker: new bounded, opt-in recorded-frame semantic index and UI; reuse ONNX CLIP; preserve exact analysis/time and authorization. Depends on source/media access checks; register lifecycle through coordinator.
3. Alert delivery worker: durable PostgreSQL outbox, bounded retry, delivery history/manual retry and UI. Own additive migration 009 and alerts service; coordinator integrates API proxy/navigation.
4. Source activation worker: reviewed, revision-bound activate/verify/rollback flow and source UI. Own ingest/source manager and camera commissioning UI/backend; coordinator integrates API proxies.
5. Coordinator: permission-checked URL navigation/resume and recording clock metadata/shared timeline. Clock provenance and unknown clocks remain explicit.
6. Integration checkpoint: reconcile contracts, meaningful focused tests, independent review, combined lint/types/unit/frontend/build checks, isolated runtime/browser checks and preserved-data verification.

### Acceptance criteria

- URLs reopen exact case/analysis/time; refresh and browser history work; dirty edits and account boundaries remain protected.
- Recorded search has explicit indexing consent/status, bounded work, real model availability, ranked timestamp results and permission checks; no hash-vector semantic claims or raw-media exposure.
- Shared timeline uses declared camera/capture clocks with correction and uncertainty; unknown timestamps are never fabricated.
- Failed outbound delivery remains persisted and retryable with stable delivery IDs and visible history; tests use only local/mock destinations.
- Source activation previews the saved configuration, verifies a fresh observation before acceptance, and supports rollback without restarting unrelated connectors; tests use authored connectors.
- Existing saved data and old evidence versions are unchanged.

### Initial findings

- Working tree clean at start; no existing approach.md or scoped AGENTS.md found.
- Existing queue recovery, saved search, rule presets, casework and live CLIP adapters will be reused rather than recreated.
- Dependencies were removed in the earlier authorized cleanup; restoring from lockfiles is necessary to run verification.

## 2026-09-23 12:56 IST — implementation and combined verification

Implemented all five workstreams locally. Recorded search uses the pinned real CLIP model, bounded samples, explicit private-original consent, private index files and exact retained-analysis links. Shared capture clocks retain operator provenance, signed correction, uncertainty, optimistic revisions and unknown clocks. URLs/resume track permitted record views without credentials; authorization changes discard loaded protected state. Durable alert delivery uses additive migration 009, atomic enqueue/escalation, leased retry claims, immutable destinations, authenticated manual retry and tenant/zone checks. Source activation previews saved configuration, verifies fresh published observations and supports rollback with cancellation-safe RTSP cleanup.

Components: API route/lifecycle integration and `recorded_search*.py` / `recording_timeline.py`; alerts service/outbox and migration 009; ingest/source activation/RTSP lifecycle; console navigation/history, case/footage callbacks and the new search/timeline/delivery/source controls. Updated README and related operations, connector, camera and review documentation; dedicated feature docs describe limits.

Independent reviews found and resolved: outbox zone-history/retry disclosure; duplicate concurrent escalation notifications; replacement of an existing clock without checking its older zone scope; editable clock fields during save; and loaded protected views surviving changed account scopes. Navigation coverage test was updated to assert both panel coverage and the shared URL-to-workspace map. The first full test invocation had seven existing local-socket tests blocked by the sandbox; the full gate was rerun with approved loopback access.

Verification evidence:

- `just --dotenv-path .env.example check`: **1,877 Python tests passed, 33 optional skips**; Ruff, library mypy, web TypeScript/build and 17 schema contract checks passed. Log `/tmp/arc-upgrade-check-final.log`.
- `npm test --prefix web`: **134 passed**; final web build/typecheck passed. Logs `/tmp/arc-upgrade-web-tests-final.log` and `/tmp/arc-upgrade-web-build-final.log`.
- **7 actual PostgreSQL outbox integration tests passed**, including concurrency, rollback, retry/recovery and zone isolation. Disposable PostgreSQL17 cluster at port55439, all migrations001–009 applied; HTTP sends mocked. Log `/tmp/sio-verify-pg-f0i4pzzn/alert-outbox-integration.log`.
- Recorded-search tests exercised real pinned ONNX CLIP and OpenCV against an authored red/blue MP4. The isolated browser API also uploaded/analyzed an authored8s clip; explicit indexing, text retrieval and sample-image retrieval produced ranked protected samples and exact retained-analysis/time links.
- Browser observed: declared clock corrected to08:34:58 UTC; uncertainty displayed; unknown clocks separate; unsaved clock navigation blocked; exact footage/case links survive refresh; Back/Forward restore locations; blocked Back retains an unsaved case summary. Source preview displays disabled configuration without contacting hardware. Alert history explicitly reports unconfigured webhook. 390px viewport has no document overflow; compact copy-link control fits.
- Preserved-state inventory: all **452 pre-existing `.sio` files** still present with unchanged size and nanosecond modification time. Existing recordings/cases/backups/models/credentials were not rewritten. Locked development dependencies and pinned CLIP assets were restored for verification; test recordings live only under a temporary runtime.

Deployment boundary: changes are local and uncommitted. No GitHub push, real-source activation, external webhook delivery, real-site accuracy, production-load or live identity-provider claim. Existing user database was not migrated. Apply additive migration009 with the normal `just db-init` workflow before restarting a real alerts deployment. CLIP native calls cannot be forcibly cancelled mid-operation, search is sampled retrieval rather than detection, and single-API-process lifecycle constraints remain documented.

Final follow-up: independent reviewer found no additional integration regressions after the permission, save-state and authorization-reset fixes. Final focused navigation/timeline checks:48 passed; final web build passed after the compact mobile link control. The isolated browser tab and test services were closed after verification; temporary evidence logs and authored fixtures are retained.

## 2026-09-23 12:58 IST — GitHub publication authorized

The user requested “push code”, authorizing publication of these five completed upgrades to `Manavp7/arc` on `main`. Reviewed the 52 changed source, test, migration and documentation files; runtime data, dependency folders, model binaries and local environment files remain excluded. Fetched `origin/main` and confirmed the remote baseline before the normal non-force push. The preceding combined checks remain the verification evidence for this implementation; GitHub Actions will provide the clean-checkout verification after publication.

## 2026-09-30 (Asia/Kolkata) — complete remaining coding backlog

User authorized all seven audited improvements and the two larger coding additions. Baseline is clean `b482ad9`. Preserve existing `.sio` data; no external publication, live camera activation, real database migration or physical actions. Development dependencies may be restored for verification and removed after checks to honor the earlier cleanup preference.

Ordered dependencies and ownership:

1. Access worker: source/zone authorization across live and recorded APIs, streams, history and complete governance audit routing; owns API app/queries/stream/timeline/video_review/video_storage authorization and shared auth helpers.
2. Operations worker: transactional alert updates, durable event deduplication, authenticated actors, paginated delivery history; workflow checkpoints, claims and safe restart recovery. Owns alerts/decision/workflow and migrations 010–011.
3. Console worker: all editor dirty guards, exact record navigation, paginated history/timeline and reviewed calibration controls. Depends on coordinator pagination/calibration contracts.
4. Coordinator: indexed workbench queries/history retention, multi-process media coordination, calibration publication and fusion reload, Python/TypeScript SDK coverage, integration and final verification. Owns store/video_jobs/evidence packages/camera commissioning/fusion/SDK and migrations 012 onward.
5. Integration: focused permission/race/recovery/navigation tests, disposable PostgreSQL where required, combined lint/type/unit/frontend/build checks, independent review and preserved-data inventory. Existing private runtime remains untouched.

Acceptance: authorization applies before exposing records/counts; simultaneous actions cannot lose acknowledgement or inflate event counts; actor identity is server-derived; crashed workflows never blindly repeat uncertain effects; older retained records remain reachable; drafts/links track all editors; SDK methods cover new workflows; cooperating API processes coordinate processing/lifecycle; calibration changes are revision-bound, reviewed, auditable and reversible with fusion acknowledgement. Real site accuracy and external provider acceptance remain unverified by coding checks.


## 2026-09-30 21:20 IST — coding backlog implemented and verified

Completed the nine authorized coding areas locally: source-derived authorization; transactional alert updates and durable event receipts; authenticated actors/full governance audit routing; checkpointed workflow recovery; indexed history/pagination/reference retention; editor draft and exact-navigation guards; typed Python/TypeScript investigation SDKs; PostgreSQL media/lifecycle coordination; and reviewed calibration publication with fusion acknowledgement and rollback. No GitHub publication, real-source activation, external webhook send or existing database migration was performed.

Components: core source-scope/PgPool helpers; API live/read-model/stream/recorded-media and workbench services; alerts/decision/workflow/MCP/fusion; console editors/history/activation/calibration controls; both SDKs; migrations010–014; focused regression suites and operational docs. Console worker took SDK ownership after initial planning. SDK OpenAPI export runs offline without application lifespan; generated declarations and snapshot drift are now included in `just check` and Linux/macOS CI. Bootstrap restores locked web and SDK dependencies, with no implicit generator downloads.

Important decisions: explicitly scoped accounts cannot access unknown source zones or whole-site endpoints lacking safe scope projection. Every stored source/version is authorized independently; immutable bookmark/report snapshots retain the union of their original source permissions. Session claims coordinate processes using the same database and private filesystem; lost claim sessions fence document puts and deletes. Heavy media decoding remains bounded to one worker. Workflow checkpoints freeze definitions/context, skip completed steps and hold uncertain external effects for human reconciliation. Calibration tickets bind actor, setup revision, source state and expiry; publication is atomic and fusion acknowledgement must match the revision. Historical workbench revisions remain intact; migration014 maintains evidence-reference protection transactionally.

Independent reviews found and fixed: scope borrowing from neighboring records; queued scoped analyses blocked by an empty optional retry snapshot; saved report/bookmark scope narrowing; report query limits before source filtering; unsafe workflow errors mistakenly considered definite failures; lost media-presence connection recovery; replacing a search row while its old executor still holds a claim; wrong-source calibration rollback; acknowledgement of invalid old poses; standalone comparison cleanup protection; mixed naive/aware timeline bounds; unsaved site pose/name/label drafts; navigation during source activation; stale job cursors after failed filter loads. Driver access was consolidated in core to preserve the architecture boundary. Initial full checks exposed outdated fixture expectations and sandbox-blocked local socket/process tests; fixtures were corrected and the final gate used approved loopback/process access.

Verification of the combined result:

- `just --dotenv-path .env.example check`: **1949 Python tests passed,33 optional skips**; Ruff/format, library mypy46 files, **160 frontend tests**, web TypeScript/production build,17 JSON schema contracts, offline OpenAPI snapshot, generated declaration drift and SDK TypeScript passed. Log `/tmp/arc-sep30-final-check.log`.
- **27 combined actual PostgreSQL regressions passed** for alerts/outbox/workflow/source scopes/media/calibration/history, plus **2 independently authored pagination/reference tests passed** against the same disposable cluster. The latter proved5001 finished jobs cannot hide older active work and all5004 records remain reachable across ties/updates. Combined PostgreSQL log: `/tmp/arc-sep30-postgres-final.log`. Independent pagination/reference verification completed with exit0 and2 passed.
- The final lost-session put/delete fence regression was rerun after extending deletion fencing: **1 passed**, `/tmp/arc-sep30-delete-fence-final.log`. SDK transport tests and mounted UI tests are included in the combined counts; all activation/delivery effects use authored or mocked inputs.
- All **457 existing `.sio` files** remained present with identical sizes and nanosecond modification timestamps, with no additions. The disposable PostgreSQL17 cluster at port55449 was stopped; normal database services were untouched.

Remaining deployment/verification boundaries: apply migrations010–014 through the normal deployment workflow before restarting upgraded services. Shared media workers require a shared filesystem and session-compatible PostgreSQL connections. Unsupported scoped surfaces intentionally return403; legacy records without source scope need authorized repair. Uncertain external workflow effects require reconciliation. No production load, physical survey/camera accuracy, live provider or end-to-end deployment qualification is claimed.

Automatic approval review rejected `npm audit` because it would transmit dependency metadata externally; that vulnerability audit was not run. The separate locked SDK generator restore was approved by the user's “do whats best u think is” response and completed. Post-verification cleanup removed47 regenerable dependency, build and cache entries, including `.venv`, both `node_modules` directories, web build output and Python caches. Source, lockfiles, generated API types and saved runtime data were preserved. Final inventory again confirmed all457 `.sio` files unchanged, no dependency/build roots remaining, and `git diff --check` passed. Cleanup receipt: `/tmp/arc-sep30-cleanup.json`. Changes remain local and uncommitted.


## 2026-09-30 21:25 IST — branch publication and pull request authorized

The user requested “push code create pr”. Publish the verified121-file coding upgrade to `Manavp7/arc` on `codex/reliability-and-investigation-upgrades`, targeting `main` through a pull request. GitHub main remains at the tested baseline `b482ad96b97cf920d4d9c3c1bfe13b000d9c5cac`; no other open PR was present. Source, tests, migrations, generated API types and documentation are included; runtime data, environment files, installed dependencies, caches and build output are excluded. Prior combined verification above remains applicable; only this publication note was added afterward. This authorizes publication, with no shared-branch merge or deployment.
