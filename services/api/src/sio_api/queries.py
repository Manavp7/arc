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
from sio_core.source_scope import ZoneScope, state_zone_sql, zone_sql
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
        allowed_zones: ZoneScope = None,
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
        scope_clause, scope_params = zone_sql(allowed_zones)
        if scope_clause:
            clauses.append(scope_clause.removeprefix(" AND "))
            params.extend(scope_params)
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

    async def entity(
        self, entity_id: str, *, tenant_id: str, allowed_zones: ZoneScope = None
    ) -> Entity | None:
        scoped, params = zone_sql(allowed_zones)
        row = await self.pool.fetchrow(
            "SELECT payload FROM entities WHERE tenant_id = %s AND entity_id = %s" + scoped,
            (tenant_id, entity_id, *params),
        )
        return Entity.model_validate(row["payload"]) if row else None

    async def entity_history(
        self, entity_id: str, *, tenant_id: str, allowed_zones: ZoneScope = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Movement history, newest first — what the timeline scrubber and analytics read."""
        scoped, params = zone_sql(allowed_zones, state_zone_sql())
        rows = await self.pool.fetch(
            f"""
            SELECT ts, ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon,
                   speed_mps, heading_deg, zone_id, confidence
              FROM entity_states
             WHERE tenant_id = %s AND entity_id = %s {scoped}
             ORDER BY ts DESC LIMIT %s
            """,
            (tenant_id, entity_id, *params, limit),
        )
        return [dict(row) for row in rows]

    async def entity_counts(
        self, *, tenant_id: str, allowed_zones: ZoneScope = None
    ) -> dict[str, int]:
        scoped, params = zone_sql(allowed_zones)
        rows = await self.pool.fetch(
            "SELECT type, count(*) AS n FROM entities WHERE tenant_id = %s"
            + scoped
            + " GROUP BY type",
            (tenant_id, *params),
        )
        return {row["type"]: int(row["n"]) for row in rows}

    # -------------------------------------------------------------------- events
    async def events(
        self,
        *,
        tenant_id: str,
        allowed_zones: ZoneScope = None,
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
        scope_clause, scope_params = zone_sql(allowed_zones)
        if scope_clause:
            clauses.append(scope_clause.removeprefix(" AND "))
            params.extend(scope_params)
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
        allowed_zones: ZoneScope = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> list[Event]:
        """Events in a window, oldest first — the order a replay wants to consume them."""
        start = start or (utc_now() - timedelta(hours=1))
        end = end or utc_now()
        scoped, params = zone_sql(allowed_zones)
        rows = await self.pool.fetch(
            "SELECT payload FROM events WHERE tenant_id = %s AND ts BETWEEN %s AND %s "
            + scoped
            + " ORDER BY ts ASC LIMIT %s",
            (tenant_id, start, end, *params, limit),
        )
        return [Event.model_validate(row["payload"]) for row in rows]

    async def world_at(
        self, ts: datetime, *, tenant_id: str, allowed_zones: ZoneScope = None, limit: int = 1000
    ) -> list[Entity]:
        """Use the same historical membership reconstruction as REST and GraphQL."""
        from .timeline import TimelineReader

        snapshot = await TimelineReader(self.pool).world_at(
            ts, tenant_id=tenant_id, allowed_zones=allowed_zones, limit=limit
        )
        return snapshot["entities"]

    # ------------------------------------------------------------------- spatial
    async def nearby(
        self,
        *,
        tenant_id: str,
        allowed_zones: ZoneScope = None,
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
        scoped, scope_params = zone_sql(allowed_zones)
        sql = f"""
            SELECT payload, ST_Distance(geom, %s::geography) AS distance_m
              FROM entities
             WHERE tenant_id = %s
               AND geom IS NOT NULL
               AND ST_DWithin(geom, %s::geography, %s)
               {type_clause} {scoped}
             ORDER BY distance_m ASC
             LIMIT %s
        """
        params: list[Any] = [point, tenant_id, point, radius_m]
        if entity_type:
            params.append(entity_type)
        params.extend(scope_params)
        params.append(limit)

        rows = await self.pool.fetch(sql, params)
        return [(Entity.model_validate(row["payload"]), float(row["distance_m"])) for row in rows]

    async def zones(
        self, *, tenant_id: str, allowed_zones: ZoneScope = None
    ) -> list[dict[str, Any]]:
        scoped, params = zone_sql(allowed_zones)
        rows = await self.pool.fetch(
            f"""
            SELECT zone_id, name, kind, restricted, capacity,
                   ST_AsGeoJSON(geom::geometry) AS geometry
              FROM zones WHERE tenant_id = %s {scoped} ORDER BY zone_id
            """,
            (tenant_id, *params),
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

    async def cameras(
        self, *, tenant_id: str, allowed_zones: ZoneScope = None
    ) -> list[dict[str, Any]]:
        """Every camera with its field of view, as GeoJSON.

        Added for the 3D twin, which draws each camera's coverage as a frustum — the one thing a 3D view shows
        that the 2D map genuinely cannot, because coverage is a volume and a flat polygon is its shadow.

        The FOV has been in the `sources` table since Phase 0 and no endpoint returned it: `cameras_covering`
        takes a zone and answers "which cameras see it" without ever handing back the geometry. So the data for
        blind-spot analysis was present and unreachable from outside the database.
        """
        scoped, params = zone_sql(allowed_zones)
        rows = await self.pool.fetch(
            f"""
            SELECT source_id, label, kind, zone_id,
                   ST_Y(geom::geometry) AS lat,
                   ST_X(geom::geometry) AS lon,
                   ST_AsGeoJSON(fov::geometry) AS fov
              FROM sources
             WHERE tenant_id = %s AND kind = 'camera' AND geom IS NOT NULL {scoped}
             ORDER BY source_id
            """,
            (tenant_id, *params),
        )
        cameras: list[dict[str, Any]] = []
        for row in rows:
            camera = dict(row)
            # Parsed here rather than in the browser. `ST_AsGeoJSON` returns a string, and leaving every client
            # to remember that is how one of them forgets and renders "[object Object]".
            camera["fov"] = json.loads(camera["fov"]) if camera.get("fov") else None
            cameras.append(camera)
        return cameras

    async def cameras_covering(
        self, *, tenant_id: str, allowed_zones: ZoneScope = None, zone_id: str
    ) -> list[dict[str, Any]]:
        """Which cameras cover a zone (PRD M6 acceptance criterion)."""
        scoped, params = zone_sql(allowed_zones, "s.zone_id")
        rows = await self.pool.fetch(
            f"""
            SELECT s.source_id, s.label,
                   ST_Y(s.geom::geometry) AS lat, ST_X(s.geom::geometry) AS lon
              FROM sources s
              JOIN zones z ON z.tenant_id = s.tenant_id AND z.zone_id = %s
             WHERE s.tenant_id = %s AND s.kind = 'camera'
               AND s.fov IS NOT NULL AND ST_Intersects(s.fov, z.geom) {scoped}
             ORDER BY s.source_id
            """,
            (zone_id, tenant_id, *params),
        )
        return [dict(row) for row in rows]

    # ---------------------------------------------------------------- timeseries
    async def measurements(
        self,
        *,
        tenant_id: str,
        allowed_zones: ZoneScope = None,
        metric: str,
        source_id: str | None = None,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses = ["tenant_id = %s", "metric = %s"]
        params: list[Any] = [tenant_id, metric]
        scope_clause, scope_params = zone_sql(allowed_zones)
        if scope_clause:
            clauses.append(scope_clause.removeprefix(" AND "))
            params.extend(scope_params)
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

    async def stats(self, *, tenant_id: str, allowed_zones: ZoneScope = None) -> dict[str, Any]:
        selections = []
        params: list[Any] = []
        tables = [
            ("entities", "entities", "count(*)", "zone_id", ""),
            ("moving_entities", "entities", "count(*)", "zone_id", " AND is_static = false"),
            ("events", "events", "count(*)", "zone_id", ""),
            ("states", "entity_states", "count(*)", state_zone_sql(), ""),
            (
                "observations",
                "observations",
                "count(*)",
                "(SELECT s.zone_id FROM sources s WHERE s.tenant_id = observations.tenant_id AND s.source_id = observations.source_id)",
                "",
            ),
            ("zones", "zones", "count(*)", "zone_id", ""),
            ("latest_entity", "entities", "max(last_seen)", "zone_id", ""),
        ]
        for name, table, aggregate, column, extra in tables:
            scoped, scope_params = zone_sql(allowed_zones, column)
            selections.append(
                f"(SELECT {aggregate} FROM {table} WHERE tenant_id = %s{extra}{scoped}) AS {name}"
            )
            params.extend([tenant_id, *scope_params])
        row = await self.pool.fetchrow("SELECT " + ", ".join(selections), params)
        return dict(row or {})
