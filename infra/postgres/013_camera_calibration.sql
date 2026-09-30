-- Reviewed, reversible calibration revisions; publishing never starts a camera connector.
ALTER TABLE sources ADD COLUMN IF NOT EXISTS calibration_revision bigint NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS camera_calibration_changes (
    tenant_id text NOT NULL REFERENCES tenants(tenant_id),
    publication_id text NOT NULL,
    source_id text NOT NULL,
    setup_id text NOT NULL,
    setup_revision integer NOT NULL,
    calibration_revision bigint NOT NULL,
    previous_pose jsonb NOT NULL,
    proposed_pose jsonb NOT NULL,
    operation text NOT NULL CHECK (operation IN ('apply', 'rollback')),
    applied_by text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, publication_id),
    UNIQUE (tenant_id, source_id, calibration_revision)
);
CREATE INDEX IF NOT EXISTS camera_calibration_setup_idx
    ON camera_calibration_changes (tenant_id, setup_id, applied_at DESC, publication_id DESC);
DROP TRIGGER IF EXISTS calibration_changes_immutable ON camera_calibration_changes;
CREATE TRIGGER calibration_changes_immutable BEFORE UPDATE OR DELETE ON camera_calibration_changes
    FOR EACH ROW EXECUTE FUNCTION refuse_workbench_history_changes();
CREATE TABLE IF NOT EXISTS camera_calibration_ack (
    tenant_id text NOT NULL,
    source_id text NOT NULL,
    calibration_revision bigint NOT NULL,
    valid boolean NOT NULL,
    acknowledged_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, source_id)
);
