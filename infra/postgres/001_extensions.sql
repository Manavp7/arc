-- Extensions. PostGIS gives SIO real spatial predicates; pgvector keeps embeddings in the
-- same tenant-scoped query as the structured data they describe (PRD §9.2 — no separate
-- vector database).
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;      -- fuzzy label search for the copilot
CREATE EXTENSION IF NOT EXISTS btree_gin;
