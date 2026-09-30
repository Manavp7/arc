-- Frozen definitions and checkpoints, separate from the public WorkflowRun schema.
CREATE TABLE IF NOT EXISTS workflow_executions (
    tenant_id text NOT NULL,
    run_id text NOT NULL,
    playbook text NOT NULL,
    subject text NOT NULL,
    trigger_event text NOT NULL,
    definition jsonb NOT NULL,
    context jsonb NOT NULL,
    checkpoint jsonb NOT NULL,
    state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','running','compensating','completed','failed','needs_human')),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id),
    UNIQUE (tenant_id, playbook, trigger_event)
);
CREATE INDEX IF NOT EXISTS workflow_recovery_idx ON workflow_executions (tenant_id, state, updated_at);
CREATE TABLE IF NOT EXISTS workflow_cooldowns (
    tenant_id text NOT NULL,
    playbook text NOT NULL,
    subject text NOT NULL,
    until_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, playbook, subject)
);
CREATE TABLE IF NOT EXISTS workflow_trigger_receipts (
    tenant_id text NOT NULL,
    playbook text NOT NULL,
    event_id text NOT NULL,
    run_id text,
    received_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, playbook, event_id)
);
