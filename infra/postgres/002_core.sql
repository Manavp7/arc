-- Core tables: tenants, sources, observations, detections, tracks, frames, embeddings.
--
-- Conventions used throughout:
--   * `tenant_id` is the first column of every primary key and every index. Multi-tenancy is
--     enforced in queries (PRD §14), and an index that does not lead with tenant_id would
--     quietly make cross-tenant scans cheap.
--   * `payload jsonb` stores the full canonical object so reads round-trip losslessly; the
--     scalar columns beside it exist purely so SQL can filter and index.
--   * `ts` columns are timestamptz. There is no such thing as a local time in a sensor feed.
--   * BRIN on time columns: these tables are append-mostly and time-ordered, which is exactly
--     the access pattern BRIN is cheapest for.

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id   text PRIMARY KEY,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    settings    jsonb NOT NULL DEFAULT '{}'::jsonb
);

INSERT INTO tenants (tenant_id, name)
VALUES ('default', 'Default tenant')
ON CONFLICT (tenant_id) DO NOTHING;

-- Registry of every signal source, so the UI can name a sensor and governance can scope one.
CREATE TABLE IF NOT EXISTS sources (
    tenant_id    text NOT NULL REFERENCES tenants(tenant_id),
    source_id    text NOT NULL,
    kind         text NOT NULL,               -- camera | gps | iot | drone | satellite | api
    modality     text NOT NULL,
    label        text,
    geom         geography(Point, 4326),       -- fixed sensors have a location
    fov          geography(Polygon, 4326),     -- cameras have a field of view (blind-spot maths)
    zone_id      text,
    config       jsonb NOT NULL DEFAULT '{}'::jsonb,
    enabled      boolean NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_seen    timestamptz,
    PRIMARY KEY (tenant_id, source_id)
);

CREATE INDEX IF NOT EXISTS sources_geom_idx ON sources USING gist (geom);
CREATE INDEX IF NOT EXISTS sources_fov_idx ON sources USING gist (fov);

CREATE TABLE IF NOT EXISTS observations (
    tenant_id       text NOT NULL,
    observation_id  text NOT NULL,
    source_id       text NOT NULL,
    modality        text NOT NULL,
    ts              timestamptz NOT NULL,
    geom            geography(PointZ, 4326),
    h3_cell         text,
    confidence      double precision NOT NULL DEFAULT 1.0,
    raw_ref         text,
    trace_id        text,
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, observation_id)
);

CREATE INDEX IF NOT EXISTS observations_ts_idx ON observations USING brin (ts);
CREATE INDEX IF NOT EXISTS observations_source_ts_idx ON observations (tenant_id, source_id, ts DESC);
CREATE INDEX IF NOT EXISTS observations_geom_idx ON observations USING gist (geom);
CREATE INDEX IF NOT EXISTS observations_trace_idx ON observations (tenant_id, trace_id);

CREATE TABLE IF NOT EXISTS detections (
    tenant_id       text NOT NULL,
    detection_id    text NOT NULL,
    observation_id  text,
    source_id       text NOT NULL,
    class_name      text NOT NULL,
    confidence      double precision NOT NULL,
    ts              timestamptz NOT NULL,
    bbox            double precision[],          -- [x1, y1, x2, y2] in source pixels
    geom            geography(Point, 4326),
    mask_ref        text,
    embedding_ref   text,
    model_name      text,
    trace_id        text,
    attrs           jsonb NOT NULL DEFAULT '{}'::jsonb,
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (tenant_id, detection_id)
);

CREATE INDEX IF NOT EXISTS detections_ts_idx ON detections USING brin (ts);
CREATE INDEX IF NOT EXISTS detections_class_idx ON detections (tenant_id, class_name, ts DESC);
CREATE INDEX IF NOT EXISTS detections_source_idx ON detections (tenant_id, source_id, ts DESC);
CREATE INDEX IF NOT EXISTS detections_observation_idx ON detections (tenant_id, observation_id);

CREATE TABLE IF NOT EXISTS tracks (
    tenant_id    text NOT NULL,
    track_id     text NOT NULL,
    source_id    text NOT NULL,
    class_name   text NOT NULL,
    status       text NOT NULL DEFAULT 'tentative',
    entity_id    text,
    start_ts     timestamptz NOT NULL,
    last_ts      timestamptz NOT NULL,
    hits         integer NOT NULL DEFAULT 0,
    confidence   double precision NOT NULL DEFAULT 1.0,
    path         geography(LineString, 4326),   -- trajectory, for movement analytics
    payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (tenant_id, track_id)
);

CREATE INDEX IF NOT EXISTS tracks_last_ts_idx ON tracks (tenant_id, last_ts DESC);
CREATE INDEX IF NOT EXISTS tracks_entity_idx ON tracks (tenant_id, entity_id);

-- Frame index: "every frame searchable" (PRD M2 acceptance criterion).
CREATE TABLE IF NOT EXISTS frames (
    tenant_id   text NOT NULL,
    frame_id    text NOT NULL,
    source_id   text NOT NULL,
    ts          timestamptz NOT NULL,
    object_key  text NOT NULL,                  -- MinIO key for the (redacted) image
    width       integer,
    height      integer,
    redacted    boolean NOT NULL DEFAULT false, -- faces/plates blurred before storage
    detections  integer NOT NULL DEFAULT 0,
    caption     text,
    trace_id    text,
    PRIMARY KEY (tenant_id, frame_id)
);

CREATE INDEX IF NOT EXISTS frames_source_ts_idx ON frames (tenant_id, source_id, ts DESC);
CREATE INDEX IF NOT EXISTS frames_ts_idx ON frames USING brin (ts);

-- Embeddings for semantic search, re-identification and agent memory.
-- 512 dimensions: CLIP ViT-B/32 and the YOLO26 ReID head both emit 512-d vectors.
CREATE TABLE IF NOT EXISTS embeddings (
    tenant_id   text NOT NULL,
    collection  text NOT NULL,                  -- frames | entities | reid | agent_memory
    item_id     text NOT NULL,
    embedding   vector(512) NOT NULL,
    metadata    jsonb NOT NULL DEFAULT '{}'::jsonb,
    ts          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, collection, item_id)
);

CREATE INDEX IF NOT EXISTS embeddings_metadata_idx ON embeddings USING gin (metadata);
-- HNSW over cosine distance: the search is always "most similar to this vector", and HNSW
-- keeps that sub-linear as the frame index grows.
CREATE INDEX IF NOT EXISTS embeddings_vector_idx
    ON embeddings USING hnsw (embedding vector_cosine_ops);
