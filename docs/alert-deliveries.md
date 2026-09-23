# Alert webhook delivery

The alerts service records a durable notification when it raises or escalates an
alert and `SIO_ALERT_WEBHOOK_URL` is set. The alert mutation and outbox insertion
commit in one PostgreSQL statement. Escalation uses an atomic prior-state predicate,
so concurrent manual and timer escalation cannot both queue the same transition.
Apply the additive
`infra/postgres/009_alert_deliveries.sql` migration through the normal `just db-init`
workflow before running this version of the alerts service.

The console's **Alert deliveries** view shows recent delivery status, attempts,
next retry, sanitized failures, and the latest 20 history entries. The list shows
up to 100 recent deliveries and supports a status filter. Endpoint labels omit
usernames, passwords, paths and query strings. Full destination URLs and immutable
payload snapshots remain private database fields and require database protection.

An unconfigured webhook is explicit in the console. Alerts produced while it is
unset do not enqueue notifications or replay automatically when it is configured.
Existing pending notifications become blocked if the configured destination
changes or is disabled; they are never silently sent to a new destination.
Restoring the original destination enables an explicit manual retry.

Every service tick (30 seconds) dispatches up to four due deliveries concurrently.
Workers claim a row with `FOR UPDATE SKIP LOCKED` and a 60-second lease. The HTTP
request has a 10-second overall deadline and does not follow redirects. A worker
that stops mid-request leaves its claim recoverable after the lease expires.
Completion is fenced with a unique lease token so stale workers cannot overwrite
a later worker's result.

Responses in the 2xx range are terminal success. Timeouts, network failures, 408,
425, 429 and 5xx responses retry with bounded exponential delays (5, 10, 20 and
40 seconds for the five-attempt cycle, subject to the service tick). Other HTTP
responses fail immediately. After five attempts, manual intervention is required.
A failed or blocked delivery can be retried by an integrator or administrator,
using a required reason. Retry resets the cycle budget but retains total attempts
and history; it records the authenticated subject. Successful, pending and
actively sending deliveries cannot be manually retried. A duplicate retry is a
409 response.

Receivers must deduplicate `Idempotency-Key` (also sent as `X-SIO-Delivery-ID` and
`delivery_id` in the JSON body). HTTP cannot guarantee exactly-once receipt: a
receiver can accept a request before the sender crashes or times out. Recovery
resends the same immutable payload with the same delivery ID. A committed success
is not dispatched again.

API gateway endpoints:

- `GET /api/alert-deliveries?status=failed&alert_id=...&limit=100` lists delivery
  history for the authenticated deployment tenant and permitted snapshot zones. Status and alert filters are
  optional; gateway maximum limit is 100. Permission: `integration.read`.
- `POST /api/alert-deliveries/{delivery_id}/retry` accepts
  `{"reason":"Receiver service repaired"}` (3–240 characters). Permission:
  `integration.write`. The API preserves the caller's identity when forwarding
  to the alerts service. Retrying a delivery outside the caller's permitted zones
  returns 404 without revealing its existence.

The separate general webhook subscription service keeps its existing behavior;
this outbox covers the alerts service's configured single destination.

Verification is split between `tests/unit/test_alert_outbox.py` (mock HTTP and
authenticated routes), `web/tests/alert-delivery.test.mjs` (UI policy/API helpers),
and `tests/integration/test_alert_outbox.py` (real PostgreSQL atomic rollback,
concurrent claims, recovery, immutable snapshots, bounded retry and tenant
isolation). Integration tests create disposable schemas and use `MockTransport`
for every outbound HTTP request. They do not demonstrate receipt at a real
external endpoint or production throughput.
