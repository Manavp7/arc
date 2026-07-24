// Neo4j schema for the SIO world model.
//
// Applied by scripts/init_neo4j.py, which splits on ';' and runs each statement. Everything
// here is IF NOT EXISTS so re-running is free.
//
// Why these specific indexes: the Neo4j adapter stores the whole entity as a JSON `payload`
// property plus scalar projections used only for filtering. Every index below backs a query
// the platform actually issues — entity lookup by id, "trucks seen in the last hour",
// zone membership, and bitemporal edge validity for timeline replay.

// One node per (tenant, entity). The composite constraint is what makes upserts safe under
// at-least-once delivery: two concurrent consumers MERGE the same entity and get one node.
CREATE CONSTRAINT entity_identity IF NOT EXISTS
FOR (e:Entity) REQUIRE (e.tenant_id, e.entity_id) IS UNIQUE;

// Type + recency: "show me every truck that entered today" (UC1).
CREATE INDEX entity_type_last_seen IF NOT EXISTS
FOR (e:Entity) ON (e.tenant_id, e.type, e.last_seen_ms);

// Recency alone, for the live map's "what is here now" query.
CREATE INDEX entity_last_seen IF NOT EXISTS
FOR (e:Entity) ON (e.tenant_id, e.last_seen_ms);

// Zone membership, for spatial questions that resolve to a named area.
CREATE INDEX entity_zone IF NOT EXISTS
FOR (e:Entity) ON (e.tenant_id, e.zone_id);

// Label lookup, for the copilot resolving "Truck ABC-123" to an entity.
CREATE INDEX entity_label IF NOT EXISTS
FOR (e:Entity) ON (e.tenant_id, e.label);

// Bitemporal edge validity. Relationship indexes are per-type in Neo4j, so index the types
// that traversals filter on by time.
CREATE INDEX rel_seen_by_validity IF NOT EXISTS
FOR ()-[r:SEEN_BY]-() ON (r.tenant_id, r.valid_from_ms, r.valid_to_ms);

CREATE INDEX rel_entered_validity IF NOT EXISTS
FOR ()-[r:ENTERED]-() ON (r.tenant_id, r.valid_from_ms, r.valid_to_ms);

CREATE INDEX rel_exited_validity IF NOT EXISTS
FOR ()-[r:EXITED]-() ON (r.tenant_id, r.valid_from_ms, r.valid_to_ms);

CREATE INDEX rel_contains_validity IF NOT EXISTS
FOR ()-[r:CONTAINS]-() ON (r.tenant_id, r.valid_from_ms, r.valid_to_ms);

CREATE INDEX rel_transporting_validity IF NOT EXISTS
FOR ()-[r:TRANSPORTING]-() ON (r.tenant_id, r.valid_from_ms, r.valid_to_ms);

CREATE INDEX rel_assigned_to_validity IF NOT EXISTS
FOR ()-[r:ASSIGNED_TO]-() ON (r.tenant_id, r.valid_from_ms, r.valid_to_ms);

CREATE INDEX rel_same_as_validity IF NOT EXISTS
FOR ()-[r:SAME_AS]-() ON (r.tenant_id, r.valid_from_ms, r.valid_to_ms);
