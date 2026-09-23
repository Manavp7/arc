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
