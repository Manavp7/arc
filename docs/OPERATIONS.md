# Operating the console

## Daily workflow

**Monitor** shows alerts, the event feed, analytics and the current site map. Live entities expire from the console after five minutes without a new observation. Connection status and the time of the latest observation remain visible. Snapshot loading retries, and a reconnected stream refreshes the snapshot.

**Investigate** brings the selected alert into an incident workspace. The recorded sequence and camera evidence belong to that incident; the affected-entity list is explicitly labelled as current state. Missions shown in this view share the alert's zone and are not claimed to be causally linked. Replay uses historical entities and events throughout. Returning to LIVE cancels outstanding replay work.

**Respond** contains decision approval, missions and the response log. Approval records an authorised option; it does not prove a physical action happened. New missions are drafts until explicitly activated. Role-aware controls reflect the server policy, which remains the final authority.

**Administration** contains sources, system health and workflow authoring. The 3D twin is optional and only loaded when opened from Investigate.

## Data and action modes

The header reports data/source mode, workflow action mode and scripted-copilot mode. Unknown or unreachable services are labelled unknown. Dry-run steps record proposed effects. The included build has no physical gate or drone command adapter, including when dry run is disabled.

System health polls actual service endpoints, showing dependency failures, pending messages, processing errors, and active adapters. A missing service has unknown queue/error counts, not zero. Historical error counters may remain nonzero after a transient failure; review the current status/checks and logs together.

## Source configuration

Sources require `integration.write` (integrator or administrator) to configure or test. Runtime listings are read-only for other authenticated roles according to policy.

1. Add a unique source ID and select its connector/modality.
2. Supply its connection options. For a camera, `kind=camera_rtsp`, `modality=video`, and `options.url=rtsp://...` or `rtsps://...`.
3. Save. Configuration is stored in `SIO_DATA_DIR/sources.json` with owner-only permissions. Responses mask secret values; unchanged masked values preserve their stored originals.
4. Test the connection. This reads at most a bounded sample and does not publish it to the live world. A successful test does not imply continuous ingest has started.
5. Restart ingestion or the full supervised stack to apply saved changes.

Enable/disable changes also require restart. The UI shows pending changes and the latest successful observation, separately from saved configuration. Existing connectors stay active under their current configuration until restarted.

Real camera bytes first enter a private `pending/` namespace. The media API rejects that namespace. Perception promotes processed frames and emits `frames.ready`; only then does the world model index a retrievable frame. No real hardware validation is implied by synthetic connector tests.

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
