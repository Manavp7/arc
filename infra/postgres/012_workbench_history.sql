-- Immutable keyset browsing and per-recording lookup. Historical evidence is retained.
CREATE INDEX IF NOT EXISTS workbench_created_page_idx
    ON workbench_documents (tenant_id, kind, created_at DESC, record_id DESC);
CREATE INDEX IF NOT EXISTS workbench_video_created_idx
    ON workbench_documents (tenant_id, kind, (payload->>'video_id'), created_at DESC, record_id DESC);
CREATE INDEX IF NOT EXISTS workbench_status_created_idx
    ON workbench_documents (tenant_id, kind, (payload->>'status'), created_at DESC, record_id DESC);
