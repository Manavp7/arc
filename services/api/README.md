# api (M22)

The platform's front door: REST, GraphQL, Server-Sent Events and media, all over one read model.

```bash
uv run python -m sio_api        # http://127.0.0.1:8000  (docs at /docs, graphql at /graphql)
```

## Why both REST and GraphQL

They answer different needs. REST is what `curl`, the SDK and simple reads use. GraphQL is for the
"these entities *with* their recent events *and* their history in one round trip" shape an operator
console actually wants, plus subscriptions. Both are backed by the same `ReadModel`, so they cannot
disagree — and the copilot's tools (Phase 4) will use it too, which means the copilot cannot answer
a question differently from the API a human is looking at.

## Routes

| | |
|---|---|
| `GET /api/entities` | filter by type, zone, recency; static infrastructure included or excluded |
| `GET /api/entities/{id}` · `/history` | one entity and its movement history |
| `GET /api/events` | filter by type, severity, entity, zone, time |
| `GET /api/timeline?from&to` | events in a window, oldest first (replay order) |
| `GET /api/world/at?ts` | **the world as it stood at an instant** (UC5) |
| `GET /api/spatial/nearby?lat&lon&radius_m` | PostGIS `ST_DWithin`, returns distances |
| `GET /api/spatial/zones` · `/coverage/{zone_id}` | site geometry; which cameras cover a zone |
| `GET /api/measurements?metric` | IoT time series |
| `GET /api/stats` | counts, per-type breakdown, stream statistics |
| `GET /stream` · `/ws` | live feed (SSE / WebSocket) |
| `GET /media/{key}` | stored frames and clips |
| `POST /graphql` | queries + subscriptions |
| `GET /health` · `/metrics` | ops |

Alerts, decisions, forecasts and missions endpoints arrive in Phase 4 with the services that
produce them.

## Two decisions worth knowing

**`/world/at` rewinds each entity's state.** It reads each entity's latest `entity_states` row at or
before the requested instant (one `DISTINCT ON` pass), rather than returning current positions with
an old entity list. Returning "the entities that existed then, where they are now" is the bug that
makes a replay useless, and it is an easy one to ship by accident.

**The live stream is a bus *tail*, not a consumer group.** One tail per process fans out to every
connected browser through an in-process hub. A consumer group would either replay the entire stream
to each new connection or move a real consumer's cursor. Slow clients get their oldest queued
messages dropped rather than being allowed to slow the tail — for a live map, newer is strictly
more useful, and SSE clients reconnect on their own.
