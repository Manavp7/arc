-- Durable delivery of the alerts service's configured outbound webhook.
-- Destination and payload are immutable snapshots. URLs may contain credentials:
-- neither this column nor payload is exposed through the delivery-history API.
CREATE TABLE IF NOT EXISTS alert_deliveries (
    tenant_id text NOT NULL,
    delivery_id text NOT NULL,
    alert_id text NOT NULL,
    action text NOT NULL CHECK (action IN ('raised', 'escalated')),
    title text NOT NULL,
    destination_url text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'delivered', 'failed', 'blocked')),
    attempts integer NOT NULL DEFAULT 0,
    cycle_attempts integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    next_attempt_at timestamptz DEFAULT now(),
    delivered_at timestamptz,
    lease_token text,
    lease_until timestamptz,
    status_code integer,
    error text,
    PRIMARY KEY (tenant_id, delivery_id)
);
CREATE INDEX IF NOT EXISTS alert_deliveries_due_idx
    ON alert_deliveries (tenant_id, next_attempt_at)
    WHERE status IN ('pending', 'sending');
CREATE INDEX IF NOT EXISTS alert_deliveries_recent_idx
    ON alert_deliveries (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS alert_delivery_history (
    history_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id text NOT NULL,
    delivery_id text NOT NULL,
    kind text NOT NULL,
    at timestamptz NOT NULL DEFAULT now(),
    attempt integer NOT NULL DEFAULT 0,
    status_code integer,
    error text,
    actor text,
    reason text,
    FOREIGN KEY (tenant_id, delivery_id)
        REFERENCES alert_deliveries (tenant_id, delivery_id)
);
CREATE INDEX IF NOT EXISTS alert_delivery_history_delivery_idx
    ON alert_delivery_history (tenant_id, delivery_id, history_id DESC);
