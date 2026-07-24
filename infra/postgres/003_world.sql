-- World model: entities, bitemporal relationships, zones.
--
-- These tables back the `postgres` GraphStore adapter and, regardless of which graph backend
-- is active, they are what the spatial engine and analytics query. PostGIS geography columns
-- are the point: "trucks within 500 m" and "cameras covering Gate B" are one index scan.

CREATE TABLE IF NOT EXISTS entities (
    tenant_id   text NOT NULL,
    entity_id   text NOT NULL,
    type        text NOT NULL,
    label       text,
    confidence  double precision NOT NULL DEFAULT 1.0,
    is_static   boolean NOT NULL DEFAULT false,
    geom        geography(Point, 4326),
    zone_id     text,
    h3_cell     text,
    first_seen  timestamptz NOT NULL,
    last_seen   timestamptz NOT NULL,
    payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, entity_id)
);

CREATE INDEX IF NOT EXISTS entities_type_idx ON entities (tenant_id, type, last_seen DESC);
CREATE INDEX IF NOT EXISTS entities_last_seen_idx ON entities (tenant_id, last_seen DESC);
CREATE INDEX IF NOT EXISTS entities_geom_idx ON entities USING gist (geom);
CREATE INDEX IF NOT EXISTS entities_zone_idx ON entities (tenant_id, zone_id);
CREATE INDEX IF NOT EXISTS entities_h3_idx ON entities (tenant_id, h3_cell);
-- Trigram index so the copilot can resolve "the red truck" against fuzzy labels.
CREATE INDEX IF NOT EXISTS entities_label_trgm_idx ON entities USING gin (label gin_trgm_ops);

-- Every state an entity has been in. Separate from `entities` (which holds the latest) so the
-- timeline can replay movement without reconstructing it from raw observations.
CREATE TABLE IF NOT EXISTS entity_states (
    tenant_id    text NOT NULL,
    entity_id    text NOT NULL,
    ts           timestamptz NOT NULL,
    geom         geography(Point, 4326),
    speed_mps    double precision,
    heading_deg  double precision,
    zone_id      text,
    h3_cell      text,
    confidence   double precision NOT NULL DEFAULT 1.0,
    payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (tenant_id, entity_id, ts)
);

CREATE INDEX IF NOT EXISTS entity_states_ts_idx ON entity_states USING brin (ts);
CREATE INDEX IF NOT EXISTS entity_states_geom_idx ON entity_states USING gist (geom);

CREATE TABLE IF NOT EXISTS relationships (
    tenant_id      text NOT NULL,
    rel_id         text NOT NULL,
    from_id        text NOT NULL,
    type           text NOT NULL,
    to_id          text NOT NULL,
    ts_valid_from  timestamptz NOT NULL,
    ts_valid_to    timestamptz,                  -- NULL = still holds
    confidence     double precision NOT NULL DEFAULT 1.0,
    payload        jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (tenant_id, rel_id),
    CONSTRAINT relationships_time_ordered CHECK (ts_valid_to IS NULL OR ts_valid_to >= ts_valid_from)
);

CREATE INDEX IF NOT EXISTS relationships_from_idx ON relationships (tenant_id, from_id, type);
CREATE INDEX IF NOT EXISTS relationships_to_idx ON relationships (tenant_id, to_id, type);
-- Partial index for the common "what holds right now" traversal.
CREATE INDEX IF NOT EXISTS relationships_open_idx
    ON relationships (tenant_id, from_id) WHERE ts_valid_to IS NULL;
CREATE INDEX IF NOT EXISTS relationships_validity_idx
    ON relationships (tenant_id, ts_valid_from, ts_valid_to);

-- Site geometry: gates, docks, yard lanes, restricted areas. Loaded from
-- infra/site/*.geojson by scripts/seed.py.
CREATE TABLE IF NOT EXISTS zones (
    tenant_id   text NOT NULL,
    zone_id     text NOT NULL,
    name        text NOT NULL,
    kind        text NOT NULL DEFAULT 'area',     -- gate | dock | lane | restricted | area
    geom        geography(Polygon, 4326) NOT NULL,
    restricted  boolean NOT NULL DEFAULT false,   -- feeds the unauthorized_entry rule
    capacity    integer,
    attributes  jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (tenant_id, zone_id)
);

CREATE INDEX IF NOT EXISTS zones_geom_idx ON zones USING gist (geom);
