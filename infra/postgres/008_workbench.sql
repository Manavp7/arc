-- Versioned operator-authored site, footage and case documents.
CREATE TABLE IF NOT EXISTS workbench_documents (
    tenant_id text NOT NULL REFERENCES tenants(tenant_id),
    kind text NOT NULL,
    record_id text NOT NULL,
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, kind, record_id)
);
CREATE INDEX IF NOT EXISTS workbench_documents_updated_idx
    ON workbench_documents (tenant_id, kind, updated_at DESC);
CREATE TABLE IF NOT EXISTS workbench_revisions (
    tenant_id text NOT NULL,
    kind text NOT NULL,
    record_id text NOT NULL,
    revision integer NOT NULL,
    payload jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, kind, record_id, revision)
);
CREATE OR REPLACE FUNCTION record_workbench_revision() RETURNS trigger AS $$
BEGIN
    INSERT INTO workbench_revisions (tenant_id, kind, record_id, revision, payload)
    VALUES (NEW.tenant_id, NEW.kind, NEW.record_id, NEW.revision, NEW.payload);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS workbench_revision_history ON workbench_documents;
CREATE TRIGGER workbench_revision_history AFTER INSERT OR UPDATE ON workbench_documents
    FOR EACH ROW EXECUTE FUNCTION record_workbench_revision();
CREATE OR REPLACE FUNCTION refuse_workbench_history_changes() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'workbench revision history is append-only';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS workbench_history_immutable ON workbench_revisions;
CREATE TRIGGER workbench_history_immutable BEFORE UPDATE OR DELETE ON workbench_revisions
    FOR EACH ROW EXECUTE FUNCTION refuse_workbench_history_changes();
