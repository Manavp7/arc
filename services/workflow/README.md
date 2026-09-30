# workflow (M15)

High-severity events select response playbooks. The service records each run and
step in PostgreSQL and publishes progress events for the console. Built-in and
authored playbooks share one durable execution path.

The inline service runner persists trigger receipts, cooldowns, frozen definitions,
per-step attempts/results and compensation state. Completed steps are skipped on
restart; only explicitly safe activities may repeat after interruption. Unknown
external effects stop for reconciliation. See
[Workflow recovery](../../docs/WORKFLOW_RECOVERY.md) for the contract and migration011.

`SIO_WORKFLOW_DRY_RUN=true` is the default. Current activities report authored
intent, read a report, or state that real gate/drone interfaces are unsupported;
a successful dry run does not prove hardware operation. Temporal is not implemented.

The standalone `InlineRunner` remains a lightweight test helper for ordering,
timeout, retry and compensation semantics. The service uses `DurableRunner`, whose
checkpoint failures propagate and prevent further effects. Progress publication
failure does not replace or discard the authoritative database record.
