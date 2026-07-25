-- Webhook delivery log (PRD M22, Phase 6).
--
-- The `webhooks` table (subscriptions) already exists in 004. This adds the log, which is the part that makes
-- outbound delivery debuggable rather than merely present: "did my webhook fire?" is the only question anybody
-- asks about a webhook, and a subscription row cannot answer it.
--
-- Deliberately NOT append-only, unlike `audit_log`. A delivery row is updated as attempts are made — attempt
-- 1 failed, attempt 2 failed, attempt 3 succeeded is one delivery with a history, not three deliveries. The
-- alternative (a row per attempt) makes "show me deliveries" a GROUP BY on every query, and the thing an
-- operator wants to see is one line per event with its outcome.

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    tenant_id      text NOT NULL,
    delivery_id    text NOT NULL,
    webhook_id     text NOT NULL,
    -- The bus topic and message that triggered this. Kept even when the subscription is later deleted, so the
    -- log survives the thing it describes — a delivery history that disappears when somebody removes a
    -- webhook is useless precisely when it is needed.
    topic          text NOT NULL,
    message_id     text,
    event_kind     text,
    url            text NOT NULL,
    status         text NOT NULL DEFAULT 'pending',   -- pending | delivered | failed | dropped
    attempts       integer NOT NULL DEFAULT 0,
    status_code    integer,
    error          text,
    -- Milliseconds for the last attempt, so a slow endpoint is visible before it starts timing out.
    duration_ms    double precision,
    created_ts     timestamptz NOT NULL DEFAULT now(),
    delivered_ts   timestamptz,
    next_retry_ts  timestamptz,
    -- The signed body, truncated. Enough to reproduce a signature mismatch, which is the single most common
    -- webhook support question, without turning the log into a second copy of every event.
    body_preview   text,
    PRIMARY KEY (tenant_id, delivery_id)
);

-- The two queries an operator actually runs: "recent deliveries for this webhook" and "what is failing".
CREATE INDEX IF NOT EXISTS webhook_deliveries_recent_idx
    ON webhook_deliveries (tenant_id, webhook_id, created_ts DESC);
CREATE INDEX IF NOT EXISTS webhook_deliveries_failing_idx
    ON webhook_deliveries (tenant_id, status, created_ts DESC)
    WHERE status IN ('failed', 'pending');

-- Retry sweep: find deliveries due for another attempt. Partial, because the overwhelming majority of rows
-- are terminal and indexing them would make this scan the whole table to find the handful that are not.
CREATE INDEX IF NOT EXISTS webhook_deliveries_retry_idx
    ON webhook_deliveries (tenant_id, next_retry_ts)
    WHERE status = 'pending';
