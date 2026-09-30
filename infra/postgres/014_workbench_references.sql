-- Inverted reference index avoids loading every historic detection for cleanup.
CREATE TABLE IF NOT EXISTS workbench_reference_edges (
    tenant_id text NOT NULL,
    kind text NOT NULL,
    record_id text NOT NULL,
    reference_id text NOT NULL,
    PRIMARY KEY (tenant_id, kind, record_id, reference_id),
    FOREIGN KEY (tenant_id, kind, record_id) REFERENCES workbench_documents ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS workbench_reference_lookup_idx
    ON workbench_reference_edges (tenant_id, reference_id, kind);
CREATE OR REPLACE FUNCTION refresh_workbench_references() RETURNS trigger AS $$
BEGIN
    DELETE FROM workbench_reference_edges WHERE tenant_id=NEW.tenant_id AND kind=NEW.kind AND record_id=NEW.record_id;
    IF NEW.kind IN ('case','evaluation_draft','evaluation_annotations','evaluation_report',
                   'evidence_package','case_comparison','review_bookmark','movement_report') THEN
        INSERT INTO workbench_reference_edges (tenant_id,kind,record_id,reference_id)
        SELECT DISTINCT NEW.tenant_id, NEW.kind, NEW.record_id, value #>> '{}'
        FROM jsonb_path_query(NEW.payload, '$.** ? (@.type() == "string")') value
        WHERE (value #>> '{}') ~ '^(vid|ana)_';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS workbench_reference_index ON workbench_documents;
CREATE TRIGGER workbench_reference_index AFTER INSERT OR UPDATE ON workbench_documents
    FOR EACH ROW EXECUTE FUNCTION refresh_workbench_references();
INSERT INTO workbench_reference_edges (tenant_id,kind,record_id,reference_id)
SELECT DISTINCT d.tenant_id,d.kind,d.record_id,value #>> '{}'
FROM workbench_documents d,
LATERAL jsonb_path_query(d.payload, '$.** ? (@.type() == "string")') value
WHERE d.kind IN ('case','evaluation_draft','evaluation_annotations','evaluation_report',
                 'evidence_package','case_comparison','review_bookmark','movement_report')
AND (value #>> '{}') ~ '^(vid|ana)_'
ON CONFLICT DO NOTHING;
