-- Reasoning and operations: events, forecasts, decisions, alerts, missions, workflows,
-- simulations, webhooks.
--
-- `events` is append-only. See 005_immutability.sql for the enforcement — the PRD requires an
-- immutable event history (§13, §14), and a table you can UPDATE is not one.

CREATE TABLE IF NOT EXISTS events (
    tenant_id    text NOT NULL,
    event_id     text NOT NULL,
    type         text NOT NULL,
    severity     text NOT NULL DEFAULT 'info',
    ts           timestamptz NOT NULL,
    detected_ts  timestamptz NOT NULL DEFAULT now(),
    geom         geography(Point, 4326),
    zone_id      text,
    entities     text[] NOT NULL DEFAULT '{}',
    source_ids   text[] NOT NULL DEFAULT '{}',
    confidence   double precision NOT NULL DEFAULT 1.0,
    rule_id      text,
    trace_id     text,
    payload      jsonb NOT NULL DEFAULT '{}'::jsonb,   -- includes the explanation object
    PRIMARY KEY (tenant_id, event_id)
);

CREATE INDEX IF NOT EXISTS events_ts_idx ON events (tenant_id, ts DESC);
CREATE INDEX IF NOT EXISTS events_ts_brin_idx ON events USING brin (ts);
CREATE INDEX IF NOT EXISTS events_type_idx ON events (tenant_id, type, ts DESC);
CREATE INDEX IF NOT EXISTS events_severity_idx ON events (tenant_id, severity, ts DESC);
CREATE INDEX IF NOT EXISTS events_entities_idx ON events USING gin (entities);
CREATE INDEX IF NOT EXISTS events_geom_idx ON events USING gist (geom);
CREATE INDEX IF NOT EXISTS events_trace_idx ON events (tenant_id, trace_id);

CREATE TABLE IF NOT EXISTS forecasts (
    tenant_id     text NOT NULL,
    forecast_id   text NOT NULL,
    target        text NOT NULL,
    entity_id     text,
    zone_id       text,
    ts            timestamptz NOT NULL,
    horizon_s     double precision NOT NULL,
    model_name    text NOT NULL DEFAULT 'unknown',
    confidence    double precision NOT NULL DEFAULT 0.5,
    payload       jsonb NOT NULL DEFAULT '{}'::jsonb,   -- points with prediction intervals
    PRIMARY KEY (tenant_id, forecast_id)
);

CREATE INDEX IF NOT EXISTS forecasts_target_idx ON forecasts (tenant_id, target, ts DESC);
CREATE INDEX IF NOT EXISTS forecasts_entity_idx ON forecasts (tenant_id, entity_id, ts DESC);

CREATE TABLE IF NOT EXISTS decisions (
    tenant_id      text NOT NULL,
    decision_id    text NOT NULL,
    trigger_event  text,
    ts             timestamptz NOT NULL,
    chosen         text,
    rationale      text NOT NULL DEFAULT '',
    confidence     double precision NOT NULL DEFAULT 0.5,
    proposed_by    text NOT NULL DEFAULT 'decision',
    approval       text NOT NULL DEFAULT 'pending',
    approved_by    text,
    approved_ts    timestamptz,
    executed_ts    timestamptz,
    solver         text,
    payload        jsonb NOT NULL DEFAULT '{}'::jsonb,   -- options + explanation
    PRIMARY KEY (tenant_id, decision_id)
);

CREATE INDEX IF NOT EXISTS decisions_ts_idx ON decisions (tenant_id, ts DESC);
CREATE INDEX IF NOT EXISTS decisions_approval_idx ON decisions (tenant_id, approval, ts DESC);
CREATE INDEX IF NOT EXISTS decisions_event_idx ON decisions (tenant_id, trigger_event);

CREATE TABLE IF NOT EXISTS alerts (
    tenant_id      text NOT NULL,
    alert_id       text NOT NULL,
    title          text NOT NULL,
    group_key      text NOT NULL,
    severity       text NOT NULL DEFAULT 'medium',
    score          double precision NOT NULL DEFAULT 0.0,
    state          text NOT NULL DEFAULT 'open',
    count          integer NOT NULL DEFAULT 1,
    ts             timestamptz NOT NULL,
    last_ts        timestamptz NOT NULL,
    geom           geography(Point, 4326),
    zone_id        text,
    event_ids      text[] NOT NULL DEFAULT '{}',
    entity_ids     text[] NOT NULL DEFAULT '{}',
    decision_ids   text[] NOT NULL DEFAULT '{}',
    ack_by         text,
    ack_ts         timestamptz,
    escalated_ts   timestamptz,
    resolved_ts    timestamptz,
    assignee       text,
    urgency_reason text,
    payload        jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (tenant_id, alert_id)
);

-- Dedup lookup: "is there already an open alert for this group?" must be a single index hit,
-- because it happens on every incoming event.
CREATE INDEX IF NOT EXISTS alerts_group_open_idx
    ON alerts (tenant_id, group_key, last_ts DESC) WHERE state IN ('open', 'escalated');
CREATE INDEX IF NOT EXISTS alerts_state_score_idx ON alerts (tenant_id, state, score DESC);
CREATE INDEX IF NOT EXISTS alerts_ts_idx ON alerts (tenant_id, ts DESC);

CREATE TABLE IF NOT EXISTS missions (
    tenant_id      text NOT NULL,
    mission_id     text NOT NULL,
    name           text NOT NULL,
    description    text,
    state          text NOT NULL DEFAULT 'draft',
    commander      text,
    zone_id        text,
    geom           geography(Point, 4326),
    assignees      text[] NOT NULL DEFAULT '{}',
    resources      text[] NOT NULL DEFAULT '{}',
    created_ts     timestamptz NOT NULL DEFAULT now(),
    updated_ts     timestamptz NOT NULL DEFAULT now(),
    started_ts     timestamptz,
    completed_ts   timestamptz,
    payload        jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (tenant_id, mission_id)
);

CREATE INDEX IF NOT EXISTS missions_state_idx ON missions (tenant_id, state, updated_ts DESC);

CREATE TABLE IF NOT EXISTS workflow_runs (
    tenant_id      text NOT NULL,
    run_id         text NOT NULL,
    playbook       text NOT NULL,
    status         text NOT NULL DEFAULT 'pending',
    trigger_event  text,
    runner         text NOT NULL DEFAULT 'temporal',
    external_id    text,
    started_ts     timestamptz NOT NULL DEFAULT now(),
    finished_ts    timestamptz,
    payload        jsonb NOT NULL DEFAULT '{}'::jsonb,   -- steps with per-step status
    PRIMARY KEY (tenant_id, run_id)
);

CREATE INDEX IF NOT EXISTS workflow_runs_status_idx ON workflow_runs (tenant_id, status, started_ts DESC);
CREATE INDEX IF NOT EXISTS workflow_runs_event_idx ON workflow_runs (tenant_id, trigger_event);

CREATE TABLE IF NOT EXISTS simulation_runs (
    tenant_id     text NOT NULL,
    run_id        text NOT NULL,
    scenario      text NOT NULL,
    status        text NOT NULL DEFAULT 'pending',
    started_ts    timestamptz NOT NULL DEFAULT now(),
    finished_ts   timestamptz,
    seeded_from   timestamptz,
    confidence    double precision NOT NULL DEFAULT 0.5,
    payload       jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (tenant_id, run_id)
);

CREATE INDEX IF NOT EXISTS simulation_runs_scenario_idx
    ON simulation_runs (tenant_id, scenario, started_ts DESC);

CREATE TABLE IF NOT EXISTS webhooks (
    tenant_id         text NOT NULL,
    webhook_id        text NOT NULL,
    url               text NOT NULL,
    topics            text[] NOT NULL DEFAULT '{}',
    secret            text,
    active            boolean NOT NULL DEFAULT true,
    created_ts        timestamptz NOT NULL DEFAULT now(),
    failure_count     integer NOT NULL DEFAULT 0,
    last_delivery_ts  timestamptz,
    last_error        text,
    PRIMARY KEY (tenant_id, webhook_id)
);

-- Time-series measurements from IoT sensors, kept relational (plain Postgres, per PRD §9.2 —
-- no TimescaleDB in Homebrew core). BRIN + the (tenant, metric, ts) index keeps the
-- forecasting queries cheap at MVP volumes.
CREATE TABLE IF NOT EXISTS measurements (
    tenant_id   text NOT NULL,
    source_id   text NOT NULL,
    metric      text NOT NULL,
    ts          timestamptz NOT NULL,
    value       double precision NOT NULL,
    unit        text,
    zone_id     text,
    entity_id   text,
    PRIMARY KEY (tenant_id, source_id, metric, ts)
);

CREATE INDEX IF NOT EXISTS measurements_metric_ts_idx ON measurements (tenant_id, metric, ts DESC);
CREATE INDEX IF NOT EXISTS measurements_ts_brin_idx ON measurements USING brin (ts);
CREATE INDEX IF NOT EXISTS measurements_entity_idx ON measurements (tenant_id, entity_id, ts DESC);
