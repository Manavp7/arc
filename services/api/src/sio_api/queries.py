"""Read-model queries.

Kept separate from the HTTP layer so the same functions back REST, GraphQL and (in Phase 4) the
copilot's tools. One implementation means the copilot cannot answer a question differently from
the API that a human is looking at — which would be the worst kind of inconsistency in a system
whose entire pitch is explainability.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sio_core import PgPool, get_pg_pool
from sio_schemas import Entity, Event, utc_now


class ReadModel:
    """Tenant-scoped reads over the relational projection of the world model."""

    def __init__(self, pool: PgPool | None = None) -> None:
        self.pool = pool or get_pg_pool()

    # ------------------------------------------------------------------ entities
    async def entities(
        self,
        *,
        tenant_id: str,
        entity_type: str | None = None,
        zone_id: str | None = None,
        since: datetime | None = None,
        active_within_s: float | None = None,
        limit: int = 200,
        offset: int = 0,
        include_static: bool = True,
    ) -> list[Entity]:
        clauses = ["tenant_id = %s"]
        params: list[Any] = [tenant_id]
        if entity_type:
            clauses.append("type = %s")
            params.append(entity_type)
        if zone_id:
            clauses.append("zone_id = %s")
            params.append(zone_id)
        if since:
            clauses.append("last_seen >= %s")
            params.append(since)
        if active_within_s:
            # Static infrastructure is exempt. A camera's `last_seen` is written once, when it is
            # registered, and never refreshed — it has nothing to report. Applying a recency window
            # to it does not hide it *temporarily*, it hides it permanently: every dock, gate, camera
            # and sensor vanished from the live map while only the zone polygons remained.
            clauses.append("(is_static OR last_seen >= %s)")
            params.append(utc_now() - timedelta(seconds=active_within_s))
        if not include_static:
            clauses.append("is_static = false")
        params.extend([limit, offset])

        rows = await self.pool.fetch(
            f"SELECT payload FROM entities WHERE {' AND '.join(clauses)} "
            "ORDER BY is_static ASC, last_seen DESC LIMIT %s OFFSET %s",
            params,
        )
        return [Entity.model_validate(row["payload"]) for row in rows]

    async def entity(self, entity_id: str, *, tenant_id: str) -> Entity | None:
        row = await self.pool.fetchrow(
            "SELECT payload FROM entities WHERE tenant_id = %s AND entity_id = %s",
            (tenant_id, entity_id),
        )
        return Entity.model_validate(row["payload"]) if row else None

    async def entity_history(
        self, entity_id: str, *, tenant_id: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Movement history, newest first — what the timeline scrubber and analytics read."""
        rows = await self.pool.fetch(
            """
            SELECT ts, ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon,
                   speed_mps, heading_deg, zone_id, confidence
              FROM entity_states
             WHERE tenant_id = %s AND entity_id = %s
             ORDER BY ts DESC LIMIT %s
            """,
            (tenant_id, entity_id, limit),
        )
        return [dict(row) for row in rows]

    async def entity_counts(self, *, tenant_id: str) -> dict[str, int]:
        rows = await self.pool.fetch(
            "SELECT type, count(*) AS n FROM entities WHERE tenant_id = %s GROUP BY type",
            (tenant_id,),
        )
        return {row["type"]: int(row["n"]) for row in rows}

    # -------------------------------------------------------------------- events
    async def events(
        self,
        *,
        tenant_id: str,
        event_type: str | None = None,
        severity: str | None = None,
        entity_id: str | None = None,
        zone_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Event]:
        clauses = ["tenant_id = %s"]
        params: list[Any] = [tenant_id]
        if event_type:
            clauses.append("type = %s")
            params.append(event_type)
        if severity:
            clauses.append("severity = %s")
            params.append(severity)
        if entity_id:
            # GIN index on the entities array makes this cheap.
            clauses.append("%s = ANY(entities)")
            params.append(entity_id)
        if zone_id:
            clauses.append("zone_id = %s")
            params.append(zone_id)
        if since:
            clauses.append("ts >= %s")
            params.append(since)
        if until:
            clauses.append("ts <= %s")
            params.append(until)
        params.extend([limit, offset])

        rows = await self.pool.fetch(
            f"SELECT payload FROM events WHERE {' AND '.join(clauses)} "
            "ORDER BY ts DESC LIMIT %s OFFSET %s",
            params,
        )
        return [Event.model_validate(row["payload"]) for row in rows]

    # ------------------------------------------------------------------ timeline
    async def timeline(
        self,
        *,
        tenant_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> list[Event]:
        """Events in a window, oldest first — the order a replay wants to consume them."""
        start = start or (utc_now() - timedelta(hours=1))
        end = end or utc_now()
        rows = await self.pool.fetch(
            "SELECT payload FROM events WHERE tenant_id = %s AND ts BETWEEN %s AND %s "
            "ORDER BY ts ASC LIMIT %s",
            (tenant_id, start, end, limit),
        )
        return [Event.model_validate(row["payload"]) for row in rows]

    async def world_at(self, ts: datetime, *, tenant_id: str, limit: int = 1000) -> list[Entity]:
        """The world as it stood at ``ts`` (PRD M8 / UC5).

        Each entity's *state at that instant* comes from `entity_states`, not from the current
        `entities` row — otherwise scrubbing back in time would show old entities at their present
        positions, which is exactly the bug that makes a replay useless.

        `DISTINCT ON` picks each entity's latest state at or before `ts` in one pass.
        """
        rows = await self.pool.fetch(
            """
            WITH state_at AS (
                SELECT DISTINCT ON (entity_id)
                       entity_id, ts,
                       ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon,
                       speed_mps, heading_deg, zone_id, confidence
                  FROM entity_states
                 WHERE tenant_id = %s AND ts <= %s
                 ORDER BY entity_id, ts DESC
            )
            SELECT e.payload, s.ts AS state_ts, s.lat, s.lon, s.speed_mps,
                   s.heading_deg, s.zone_id, s.confidence
              FROM entities e
              LEFT JOIN state_at s ON s.entity_id = e.entity_id
             WHERE e.tenant_id = %s
               AND e.first_seen <= %s
               AND (e.is_static OR s.entity_id IS NOT NULL)
             ORDER BY e.is_static ASC, e.last_seen DESC
             LIMIT %s
            """,
            (tenant_id, ts, tenant_id, ts, limit),
        )

        entities: list[Entity] = []
        for row in rows:
            entity = Entity.model_validate(row["payload"])
            if row["lat"] is not None and row["lon"] is not None:
                # Rewind the entity to its historical state.
                from sio_schemas import EntityState, Geo

                entity = entity.model_copy(
                    update={
                        "state": EntityState(
                            ts=row["state_ts"],
                            geo=Geo(lat=float(row["lat"]), lon=float(row["lon"])),
                            heading_deg=row["heading_deg"],
                            zone_id=row["zone_id"],
                            confidence=float(row["confidence"] or 1.0),
                        ),
                        "last_seen": row["state_ts"],
                    }
                )
            entities.append(entity)
        return entities

    # ------------------------------------------------------------------- spatial
    async def nearby(
        self,
        *,
        tenant_id: str,
        lat: float,
        lon: float,
        radius_m: float,
        entity_type: str | None = None,
        limit: int = 100,
    ) -> list[tuple[Entity, float]]:
        """Entities within ``radius_m``, nearest first, with their distance.

        `ST_DWithin` on a geography column with a GiST index — a real spatial query, which is the
        point of putting PostGIS underneath (PRD M6).
        """
        # Parameters are assembled strictly in the order they appear in the SQL text. Clever
        # re-slicing of a params list is how the Postgres graph adapter ended up binding tenant_id
        # into a JOIN predicate and silently returning wrong rows; not repeating that here.
        point = f"SRID=4326;POINT({lon} {lat})"
        type_clause = "AND type = %s" if entity_type else ""
        sql = f"""
            SELECT payload, ST_Distance(geom, %s::geography) AS distance_m
              FROM entities
             WHERE tenant_id = %s
               AND geom IS NOT NULL
               AND ST_DWithin(geom, %s::geography, %s)
               {type_clause}
             ORDER BY distance_m ASC
             LIMIT %s
        """
        params: list[Any] = [point, tenant_id, point, radius_m]
        if entity_type:
            params.append(entity_type)
        params.append(limit)

        rows = await self.pool.fetch(sql, params)
        return [(Entity.model_validate(row["payload"]), float(row["distance_m"])) for row in rows]

    async def zones(self, *, tenant_id: str) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT zone_id, name, kind, restricted, capacity,
                   ST_AsGeoJSON(geom::geometry) AS geometry
              FROM zones WHERE tenant_id = %s ORDER BY zone_id
            """,
            (tenant_id,),
        )
        import json

        return [
            {
                "zone_id": row["zone_id"],
                "name": row["name"],
                "kind": row["kind"],
                "restricted": row["restricted"],
                "capacity": row["capacity"],
                "geometry": json.loads(row["geometry"]) if row["geometry"] else None,
            }
            for row in rows
        ]

    async def cameras(self, *, tenant_id: str) -> list[dict[str, Any]]:
        """Every camera with its field of view, as GeoJSON.

        Added for the 3D twin, which draws each camera's coverage as a frustum — the one thing a 3D view shows
        that the 2D map genuinely cannot, because coverage is a volume and a flat polygon is its shadow.

        The FOV has been in the `sources` table since Phase 0 and no endpoint returned it: `cameras_covering`
        takes a zone and answers "which cameras see it" without ever handing back the geometry. So the data for
        blind-spot analysis was present and unreachable from outside the database.
        """
        rows = await self.pool.fetch(
            """
            SELECT source_id, label, kind, zone_id,
                   ST_Y(geom::geometry) AS lat,
                   ST_X(geom::geometry) AS lon,
                   ST_AsGeoJSON(fov::geometry) AS fov,
                   config
              FROM sources
             WHERE tenant_id = %s AND kind = 'camera' AND geom IS NOT NULL
             ORDER BY source_id
            """,
            (tenant_id,),
        )
        cameras: list[dict[str, Any]] = []
        for row in rows:
            camera = dict(row)
            # Parsed here rather than in the browser. `ST_AsGeoJSON` returns a string, and leaving every client
            # to remember that is how one of them forgets and renders "[object Object]".
            camera["fov"] = json.loads(camera["fov"]) if camera.get("fov") else None
            cameras.append(camera)
        return cameras

    async def cameras_covering(self, *, tenant_id: str, zone_id: str) -> list[dict[str, Any]]:
        """Which cameras cover a zone (PRD M6 acceptance criterion)."""
        rows = await self.pool.fetch(
            """
            SELECT s.source_id, s.label,
                   ST_Y(s.geom::geometry) AS lat, ST_X(s.geom::geometry) AS lon
              FROM sources s
              JOIN zones z ON z.tenant_id = s.tenant_id AND z.zone_id = %s
             WHERE s.tenant_id = %s AND s.kind = 'camera'
               AND s.fov IS NOT NULL AND ST_Intersects(s.fov, z.geom)
             ORDER BY s.source_id
            """,
            (zone_id, tenant_id),
        )
        return [dict(row) for row in rows]

    # ---------------------------------------------------------------- timeseries
    async def measurements(
        self,
        *,
        tenant_id: str,
        metric: str,
        source_id: str | None = None,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses = ["tenant_id = %s", "metric = %s"]
        params: list[Any] = [tenant_id, metric]
        if source_id:
            clauses.append("source_id = %s")
            params.append(source_id)
        if since:
            clauses.append("ts >= %s")
            params.append(since)
        params.append(limit)
        rows = await self.pool.fetch(
            f"SELECT source_id, metric, ts, value, unit, zone_id FROM measurements "
            f"WHERE {' AND '.join(clauses)} ORDER BY ts DESC LIMIT %s",
            params,
        )
        return [dict(row) for row in rows]

    async def stats(self, *, tenant_id: str) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            SELECT (SELECT count(*) FROM entities WHERE tenant_id = %s) AS entities,
                   (SELECT count(*) FROM entities WHERE tenant_id = %s AND is_static = false)
                       AS moving_entities,
                   (SELECT count(*) FROM events WHERE tenant_id = %s) AS events,
                   (SELECT count(*) FROM entity_states WHERE tenant_id = %s) AS states,
                   (SELECT count(*) FROM observations WHERE tenant_id = %s) AS observations,
                   (SELECT count(*) FROM zones WHERE tenant_id = %s) AS zones,
                   (SELECT max(last_seen) FROM entities WHERE tenant_id = %s) AS latest_entity
            """,
            (tenant_id,) * 7,
        )
        return dict(row or {})
