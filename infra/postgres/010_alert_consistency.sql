-- A receipt is committed in the same transaction as the resulting alert mutation.
-- This survives the consumer's process-local cache and prevents redelivery counts.
CREATE TABLE IF NOT EXISTS alert_consumed_events (
    tenant_id text NOT NULL,
    event_id text NOT NULL,
    alert_id text,
    consumed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, event_id)
);
CREATE INDEX IF NOT EXISTS alert_consumed_events_alert_idx
    ON alert_consumed_events (tenant_id, alert_id);
-- Preserve the retained event IDs when upgrading an existing deployment.
INSERT INTO alert_consumed_events (tenant_id, event_id, alert_id)
SELECT tenant_id, event_id, min(alert_id)
FROM alerts CROSS JOIN LATERAL unnest(event_ids) AS retained(event_id)
GROUP BY tenant_id, event_id
ON CONFLICT (tenant_id, event_id) DO NOTHING;
