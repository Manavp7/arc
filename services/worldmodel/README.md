# worldmodel (M7)

The heart of the platform: consumes `entities` and `tracks`, and maintains the live
spatiotemporal world model — a graph of entities and **bitemporal** relationships, plus the
relational projection that the spatial engine and analytics query.

```
entities ──┐
tracks  ───┴──► worldmodel ──► GraphStore (Neo4j | Postgres)
                            ├─► Postgres: entities, entity_states, relationships
                            └─► VectorStore (Phase 2: CLIP embeddings for semantic search)
```

## What it guarantees

- **Upsert means merge, never replace.** `first_seen` only ever moves earlier and `last_seen` only
  later, so a replayed or out-of-order message cannot shrink an entity's known lifetime.
- **Relationships are closed, not deleted.** When a truck leaves a dock, the `CONTAINS` edge gets
  a `ts_valid_to`; the edge itself stays. That is what makes "reconstruct the world as it was at
  T" possible (PRD M8, UC5) rather than "here is what is true now".
- **Every state is kept.** `entity_states` holds the full movement history, so replay and
  movement analytics do not have to re-derive it from raw observations.
- **Idempotent.** At-least-once delivery means the same entity arrives twice; the handler is safe
  under repetition (verified in the unit tests).

## Endpoints

`/health` reports graph reachability, entity/relationship counts and consumer lag.
`/counts` is a cheap summary used by the demo and by `just doctor`.

## Run

```bash
uv run python -m sio_worldmodel
```
