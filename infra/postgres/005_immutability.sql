-- Audit log and append-only enforcement.
--
-- The PRD asks for an "immutable audit + event history" (§13) and "append-only audit stream +
-- table" (§14). A table that merely *convention* says is append-only is not one, so this is
-- enforced by triggers that fire regardless of who is connected — including the local
-- superuser the dev stack runs as, which is exactly the case a REVOKE would miss.
--
-- Retention still needs to delete eventually (§13). Rather than leaving a hole, deletion is
-- gated on a session flag that only the retention job sets, and setting it is itself an
-- audited action:
--
--     SET LOCAL sio.retention_job = 'on';
--     DELETE FROM events WHERE ts < now() - interval '365 days';

CREATE TABLE IF NOT EXISTS audit_log (
    tenant_id     text NOT NULL,
    audit_id      text NOT NULL,
    ts            timestamptz NOT NULL DEFAULT now(),
    actor         text NOT NULL,
    actor_roles   text[] NOT NULL DEFAULT '{}',
    action        text NOT NULL,
    resource      text,
    allowed       boolean NOT NULL DEFAULT true,
    reason        text,
    policy_engine text,
    ip            text,
    user_agent    text,
    request_id    text,
    trace_id      text,
    details       jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (tenant_id, audit_id)
);

CREATE INDEX IF NOT EXISTS audit_log_ts_idx ON audit_log (tenant_id, ts DESC);
CREATE INDEX IF NOT EXISTS audit_log_actor_idx ON audit_log (tenant_id, actor, ts DESC);
CREATE INDEX IF NOT EXISTS audit_log_action_idx ON audit_log (tenant_id, action, ts DESC);
-- Denials are the rows a compliance review actually reads, so give them their own index.
CREATE INDEX IF NOT EXISTS audit_log_denied_idx
    ON audit_log (tenant_id, ts DESC) WHERE allowed = false;
CREATE INDEX IF NOT EXISTS audit_log_trace_idx ON audit_log (tenant_id, trace_id);

CREATE OR REPLACE FUNCTION sio_forbid_mutation() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' AND coalesce(current_setting('sio.retention_job', true), '') = 'on' THEN
        RETURN OLD;   -- retention job: deletion permitted, and logged by the job itself
    END IF;
    RAISE EXCEPTION
        '% on %.% is forbidden: this table is append-only (SIO governance)',
        TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
        USING HINT = 'correct history by appending a new row; retention must set sio.retention_job';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log;
CREATE TRIGGER audit_log_append_only
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION sio_forbid_mutation();

DROP TRIGGER IF EXISTS events_append_only ON events;
CREATE TRIGGER events_append_only
    BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION sio_forbid_mutation();

-- Observations and detections are raw evidence: an explanation that cites a detection must be
-- able to trust that the detection has not been edited since.
DROP TRIGGER IF EXISTS observations_append_only ON observations;
CREATE TRIGGER observations_append_only
    BEFORE UPDATE OR DELETE ON observations
    FOR EACH ROW EXECUTE FUNCTION sio_forbid_mutation();

DROP TRIGGER IF EXISTS detections_append_only ON detections;
CREATE TRIGGER detections_append_only
    BEFORE UPDATE OR DELETE ON detections
    FOR EACH ROW EXECUTE FUNCTION sio_forbid_mutation();

-- Schema bookkeeping for scripts/init_db.py.
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    text PRIMARY KEY,
    checksum    text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
