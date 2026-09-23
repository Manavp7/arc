# Reviewed source activation

Saved source edits remain drafts until activated or ingestion restarts. In **Sources**, choose **Preview activation** to compare the running configuration with the saved draft, then **Activate and verify incoming data**. Camera Setup exposes the same flow for its selected RTSP source. Source mutations require `integration.write` and the deployment tenant.

A preview expires after five minutes and binds the unredacted saved configuration, its revision, current running configuration, and rollback checkpoint. Its public fields are redacted. Saving again, changing credentials behind a mask, applying another change, or restarting ingestion invalidates older tickets. Tickets are one-use, and activation, connection tests and configuration writes cannot overlap an activation transaction.

Activation stops only the selected connector. A sample verifies activation only when it is from that exact replacement connector, matches the configured source and modality, has a timestamp at or after activation began (not more than five seconds in the future), and was successfully published to the bus. An RTSP source that stores frames must also produce a media reference. Frame receipt does not prove sanitization, coverage, positioning, detector accuracy, or the availability of downstream consumers. A disabled source is reported as disabled, without a verified-sample claim.

A mode-0600 checkpoint in `sources.json` is written before the running connection changes. Failed activation or request cancellation restores and saves the prior running configuration and verifies a new observation from the restored connector. A first activation with no prior running connector is saved disabled on failure. If rollback itself fails, the UI reports `rollback_failed` and blocks further activation for that source until ingestion restarts. On process restart, an interrupted applying/recovering checkpoint restores the prior configuration before connector construction, without carrying forward a claim of fresh verification. Successfully applied changes expose **Preview rollback**, including after an ingestion restart.

The requested sample timeout is 3–30 seconds (20 by default); recovery waits up to 12 seconds for a new sample. Cooperative connector startup/pump cancellation and cleanup each have a five-second deadline. A connector that refuses cancellation is retained and blocks replacement instead of starting another connection beside it. Plugin implementations must keep `start`, `stop` and `observations` asynchronous and cancellation-safe; synchronous blocking plugin code cannot be forcibly interrupted by an asyncio service. The RTSP adapter releases captures whose OpenCV open completes after cancellation, and serializes capture release with an in-flight reader. The built-in simulator remains restart-managed because its ground-truth publisher has a separate lifecycle.

Measured camera readiness and connection activation are separate. Activation applies saved connection and sampling settings. It does **not** write the measured pose into the fusion calibration table or claim that the survey is independently validated. The Camera Setup panel states this at the activation controls.

## HTTP contract

These paths are served by ingestion and proxied beneath `/api` by the gateway:

- `GET /sources/{source_id}/activation`: sanitized latest activation result.
- `POST /sources/{source_id}/activation/preview`: short-lived preview of saved settings.
- `POST /sources/{source_id}/activation`: apply `{ "preview_id": "…", "timeout_s": 20 }`.
- `POST /sources/{source_id}/activation/rollback-preview`: review the prior running settings.
- `POST /sources/{source_id}/activation/rollback`: apply the rollback preview using the same body shape.

A lost HTTP response is not proof that an operation was abandoned. Reload source activation status before retrying. The server completes recovery independently of a disconnected caller. API responses never include private rollback configuration or exception text from device libraries.

## Verification scope

`tests/unit/test_source_activation.py` uses authored in-process connectors and an in-memory bus. It exercises publication, freshness, unrelated-source isolation, stale/replayed tickets, disabled state, failure/cancellation recovery, persisted rollback, crash recovery, storage failures, tenant/role restrictions, and credential masking. `web/tests/source-activation.test.mjs` covers action wording, source path encoding and failed-recovery presentation. These checks do not activate user hardware or prove RTSP device compatibility.
