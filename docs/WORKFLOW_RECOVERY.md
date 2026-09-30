# Workflow recovery

Apply migration 011 using the normal database migration command before running
this service version. `SIO_WORKFLOW_RUNNER=inline` now uses PostgreSQL admission
receipts, per-subject cooldowns, frozen playbook definitions and step checkpoints.
Temporal remains unsupported; selecting it fails explicitly.

A trigger/playbook pair receives a deterministic run ID. Admission, its cooldown,
trigger receipt and initial run record commit together. Concurrent consumers
cannot admit the same trigger twice or race the same cooldown. Suppressed events
also receive a receipt, so replay after the cooldown expires does not start an
old suppressed event. Manual starts respect cooldowns too.

One live executor owns a run through a PostgreSQL session advisory lock. The lock
is held on the same connection used to checkpoint; another process cannot claim
that run while it remains held. A lost session releases ownership. Before each
activity or compensation, the executor persists the exact in-flight step and
attempt. Completed results and public run status commit together. Checkpoint
failure stops execution before another activity is attempted.

The service scans queued/interrupted runs every 60 seconds. Recovery uses the
saved definition, arguments, dry-run setting and context, not a newly edited
playbook. Completed steps are skipped. A step interrupted in flight can repeat
only when its actual registered implementation explicitly declares
`recovery_safe = True`, and its persisted retry budget remains bounded. Safe
compensations are also checkpointed and completed compensations are skipped.

The shipped activities only compute, GET a report, record intent or report that
an actuator is unsupported. They explicitly declare this recovery property.
Adding a real gate, drone or notification integration requires removing the safe
flag until receiver-side idempotency is implemented and verified. A context's
in-memory result cache is not sufficient evidence of external idempotency.

An unknown in-flight activity, or an exception/timeout or returned error from an activity without
the recovery contract, stops in `needs_human`. The public run status is failed,
with the uncertain activity retained and a reconciliation explanation. It is
not automatically retried or compensated: an unknown result may already have
acted. Reconcile the external system before considering a fresh manual run.
There is intentionally no blind “retry uncertain effect” endpoint. Failed
compensation also remains visible as requiring reconciliation.

`GET /workflow/runs` reads saved runs after restart, and each entry/detail includes
`recovery_state` and `needs_human`. Explicit zone scopes filter against the saved
trigger zone; unknown-zone runs are denied. Older runs created before migration 011
lack a frozen definition/checkpoint and cannot automatically resume; their history
remains readable by an unrestricted principal. PostgreSQL checkpoints do not
retroactively reconstruct previously lost process memory.

Run and step records are authoritative. Published progress events are best-effort
observability: a bus interruption can omit a progress event without discarding a
checkpoint. Recovery does not claim exactly-once behavior from external systems.
No live actuator is implemented by this change.

Report activities query events within the saved trigger zone before applying the
result limit, and reject mismatched tenant or zone responses. Unknown-zone runs
record an explicit unavailable report without querying tenant-wide events.
Published step and summary events carry that same saved source zone.

Verification: `tests/unit/test_workflow_recovery.py` exercises checkpoint failures,
completed-step skip, safe resume, uncertain timeouts and compensation interruption.
`tests/integration/test_workflow_recovery.py` uses disposable PostgreSQL schemas
for actual concurrent admission/claims, persisted cooldowns/receipts and recovery
through a fresh store. Activities in these tests are authored local functions;
no hardware or external delivery is involved.
