# Operating the console

## Daily workflow

**Monitor** shows alerts, the event feed, analytics and the current site map. Live entities expire from the console after five minutes without a new observation. Connection status and the time of the latest observation remain visible. Snapshot loading retries, and a reconnected stream refreshes the snapshot.

**Investigate** brings the selected alert into an incident workspace. The recorded sequence and camera evidence belong to that incident; the affected-entity list is explicitly labelled as current state. Missions shown in this view share the alert's zone and are not claimed to be causally linked. Replay uses historical entities and events throughout. Returning to LIVE cancels outstanding replay work.

**Respond** contains decision approval, missions and the response log. Approval records an authorised option; it does not prove a physical action happened. New missions are drafts until explicitly activated. Role-aware controls reflect the server policy, which remains the final authority.

**Administration** contains sources, system health and workflow authoring. The 3D twin is optional and only loaded when opened from Investigate.

**Footage & cases** includes Visual search for opt-in local CLIP retrieval and Camera timeline for a shared view of declared recording clocks. The top bar's **Copy view link** retains a case or exact recording analysis/position; refresh, Back/Forward and browser-local per-account resume reopen authorized records. Unsaved investigation drafts remain protected and are not included in links. See [investigation navigation and capture clocks](INVESTIGATION_NAVIGATION.md) and [recorded visual search](RECORDED_SEARCH.md).

## Data and action modes

The header reports data/source mode, workflow action mode and scripted-copilot mode. Unknown or unreachable services are labelled unknown. Dry-run steps record proposed effects. The included build has no physical gate or drone command adapter, including when dry run is disabled.

System health polls actual service endpoints, showing dependency failures, pending messages, processing errors, and active adapters. A missing service has unknown queue/error counts, not zero. Historical error counters may remain nonzero after a transient failure; review the current status/checks and logs together.

## Source configuration

Sources require `integration.write` (integrator or administrator) to configure or test. Runtime listings are read-only for other authenticated roles according to policy.

1. Add a unique source ID and select its connector/modality.
2. Supply its connection options. For a camera, `kind=camera_rtsp`, `modality=video`, and `options.url=rtsp://...` or `rtsps://...`.
3. Save. Configuration is stored in `SIO_DATA_DIR/sources.json` with owner-only permissions. Responses mask secret values; unchanged masked values preserve their stored originals.
4. Test the connection. This reads at most a bounded sample and does not publish it to the live world. A successful test does not imply continuous ingest has started.
5. Choose **Preview activation**, inspect the redacted running and saved configurations, then **Activate and verify incoming data**. This replaces only the selected connector and requires a fresh observation from that replacement to be successfully published. The preview is revision-bound and expires; saving another edit requires a new preview.
6. Read the activation result. Failed activation attempts restoration of the prior configuration. **Preview rollback** allows an explicit reviewed return to the retained prior configuration. If recovery fails, inspect the reported failure before restarting ingestion; do not assume that a disconnected browser cancelled server recovery.

Enable/disable changes follow the same saved-configuration flow. Saving alone leaves existing connectors on their current configuration; ingestion restart also applies saved settings. The built-in simulator remains restart-managed. Camera setup exposes activation controls separately from measured readiness; save or discard setup edits first. A disabled source has no fresh-observation verification claim. See [SOURCE_ACTIVATION.md](SOURCE_ACTIVATION.md) for checkpoint persistence, rollback, timeouts and connector cancellation requirements.

Real camera bytes first enter a private `pending/` namespace. The media API rejects that namespace. Perception promotes processed frames and emits `frames.ready`; only then does the world model index a retrievable frame. No real hardware validation is implied by synthetic connector tests.

## Recorded search and clocks

Recorded visual search requires the installed pinned CLIP image/text/tokenizer assets and explicit consent for each original recording. Embeddings are local private files, not API responses or append-only document history. Revisions or original-media changes invalidate the index; the operator must review and rebuild it. Removing an index preserves the recording, while purge removes the containing media directory. The indexer shares native video-processing capacity through PostgreSQL session claims across cooperating API workers on one shared media filesystem. See [RECORDED_SEARCH.md](RECORDED_SEARCH.md) for limits and installation.

Camera timeline uses operator-declared capture time plus signed correction. Enter a timezone and document the measurement source. Blank capture time remains unknown, and blank uncertainty remains unknown rather than zero. Uncertainty is displayed as a qualification; it is not automatic clock synchronization or proof of overlap. Existing case snapshots and comparison offsets are unchanged. See [INVESTIGATION_NAVIGATION.md](INVESTIGATION_NAVIGATION.md#camera-timeline).

## Outbound alert delivery

Apply migration `009_alert_deliveries.sql` through the normal database migration workflow before starting the updated alerts service. When `SIO_ALERT_WEBHOOK_URL` is configured, raising/escalating an alert atomically queues its immutable notification in PostgreSQL. **Administration → Alert delivery** displays status, attempts, sanitized failures and recent history for permitted source zones. Eligible manual retries require `integration.write` and an explanation.

Automatic retries are bounded; interrupted requests recover after their lease expires. A changed or disabled destination blocks pending deliveries instead of forwarding them elsewhere. Alerts created with no configured destination are not retrospectively queued. Receivers must deduplicate the stable delivery ID because an unconfirmed HTTP attempt may already have been accepted. The separate general webhook subscription service is unchanged. See [alert-deliveries.md](alert-deliveries.md) for retry timing, access and receiver requirements. Local mocked endpoints verify the protocol path, not actual third-party delivery or production throughput.

## Authentication

Development mode provides an explicitly labelled test-identity selector. It is not a production identity provider.

For Keycloak, configure these settings together:

```dotenv
SIO_AUTH_MODE=keycloak
SIO_OIDC_DISCOVERY_URL=https://identity.example/realms/site/.well-known/openid-configuration
SIO_OIDC_AUDIENCE=sio-api
SIO_KEYCLOAK_CLIENT_ID=sio-console
```

Register the console as a public OIDC client with authorization code flow and PKCE S256, the exact console origin/redirect URI, appropriate web origins/CORS, and post-logout redirect URI. Issue the API audience and the tenant/roles/clearance claims expected by `sio_core.authn`. Do not put a client secret in the browser.

The browser discovers configuration from `/auth/config`, validates the stored OAuth state, exchanges the code with its PKCE verifier, renews expiring sessions, and clears authentication on logout. API calls and event streams use bearer headers. OIDC protocol behavior is regression-tested against a mock provider; real deployment registration and login must be verified against the chosen provider.

Backend WebSockets validate identity and origin, and streaming sessions expire with their token. Domain services reject callers from a different deployment tenant; deploy a separate service stack per tenant until service query scoping is expanded and verified.

## Execution and extension limits

Inline workflows implement retries, step timeouts, compensation and recorded progress. They do not recover in-flight execution after process death. `SIO_WORKFLOW_RUNNER=temporal` is rejected until that runner is implemented.

GPU profile entries documented as stubs refuse real work. Keep the CPU/default stack for operational verification. Additional connectors, autonomous agents and workflow authoring features should be expanded only after the existing acceptance scenario and relevant real-source tests pass.

## Reliability and history upgrade

Apply migrations 010–014 with `just db-init` before restarting upgraded services. These add alert input receipts, durable workflow checkpoints, history indexes, reviewed calibration publication/acknowledgement and transactional evidence-reference indexing. They preserve existing media, workbench rows and immutable history. See [workflow recovery](WORKFLOW_RECOVERY.md), [zone access](ZONE_ACCESS.md), [camera commissioning](CAMERA_COMMISSIONING.md) and [video review](VIDEO_REVIEW.md).

Use session-preserving PostgreSQL connections for media/workflow claims; transaction-pooling proxies are incompatible with session advisory locks. Multiple API processes require the same private media filesystem and database. Heavy decoding remains intentionally serialized for bounded local memory use. Live deployment sizing and multi-host storage are separate deployment work.
