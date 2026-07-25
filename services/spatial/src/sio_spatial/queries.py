"""PostGIS-backed spatial queries (PRD M6).

These answer the questions the acceptance criteria name — "trucks within 500 m", "nearest hospital",
"cameras covering Gate B" — and they run in the database rather than in Python because that is where
the geometry and the GIST indexes are. Pulling every entity into the process to filter it would work
at demo scale and fall over at the first real site.

Note the consistent use of ``geography`` rather than ``geometry``: distances come back in metres on the
spheroid instead of in degrees, and a query for "within 500 m" that silently means "within 500 degrees
of longitude" is the kind of bug that returns plausible answers.
"""

from __future__ import annotations

import json
from typing import Any

from sio_core import PgPool, get_logger
from sio_schemas import Geo

from .geometry import CameraFootprint, ZoneShape, cell_for, zone_shape_from_row

log = get_logger("sio.spatial.queries")


class SpatialQueries:
    """The read model for spatial questions."""

    def __init__(self, pool: PgPool, tenant_id: str) -> None:
        self.pool = pool
        self.tenant_id = tenant_id

    # -------------------------------------------------------------------- zones
    async def load_zones(self) -> list[ZoneShape]:
        """Every zone, as shapely geometry for the in-memory index."""
        rows = await self.pool.fetch(
            """
            SELECT zone_id, name, kind, restricted, capacity, attributes,
                   ST_AsGeoJSON(geom::geometry) AS geojson
              FROM zones
             WHERE tenant_id = %s
            """,
            (self.tenant_id,),
        )
        shapes = []
        for row in rows:
            record = dict(row)
            if isinstance(record.get("geojson"), str):
                record["geojson"] = json.loads(record["geojson"])
            shape_ = zone_shape_from_row(record)
            if shape_ is not None:
                shapes.append(shape_)
        return shapes

    async def load_camera_footprints(self) -> list[CameraFootprint]:
        """Camera fields of view as ground polygons, for coverage and blind spots."""
        rows = await self.pool.fetch(
            """
            SELECT source_id, config,
                   ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon
              FROM sources
             WHERE tenant_id = %s AND kind = 'camera' AND geom IS NOT NULL
            """,
            (self.tenant_id,),
        )
        footprints = []
        for row in rows:
            config = row.get("config") or {}
            footprints.append(
                CameraFootprint.build(
                    source_id=str(row["source_id"]),
                    geo=Geo(lat=float(row["lat"]), lon=float(row["lon"])),
                    bearing_deg=float(config.get("bearing_deg", 0.0)),
                    fov_deg=float(config.get("fov_deg", 70.0)),
                    range_m=float(config.get("range_m", 60.0)),
                )
            )
        return footprints

    # ---------------------------------------------------------------- entities
    async def within_radius(
        self,
        geo: Geo,
        radius_m: float,
        *,
        entity_type: str | None = None,
        active_within_s: float | None = 300.0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Entities within a radius, nearest first. The "trucks within 500 m" query."""
        clauses = [
            "e.tenant_id = %s",
            "e.geom IS NOT NULL",
            "ST_DWithin(e.geom, %s::geography, %s)",
        ]
        params: list[Any] = [self.tenant_id, _point(geo), radius_m]
        if entity_type:
            clauses.append("e.type = %s")
            params.append(entity_type)
        if active_within_s is not None:
            clauses.append("(e.is_static OR e.last_seen >= now() - make_interval(secs => %s))")
            params.append(active_within_s)
        params.append(limit)
        rows = await self.pool.fetch(
            f"""
            SELECT e.entity_id, e.type, e.label, e.is_static, e.last_seen,
                   ST_Y(e.geom::geometry) AS lat, ST_X(e.geom::geometry) AS lon,
                   ST_Distance(e.geom, %s::geography) AS distance_m
              FROM entities e
             WHERE {" AND ".join(clauses)}
             ORDER BY distance_m ASC
             LIMIT %s
            """,
            (_point(geo), *params),
        )
        return [
            {
                "entity_id": row["entity_id"],
                "type": row["type"],
                "label": row["label"],
                "is_static": row["is_static"],
                "distance_m": round(float(row["distance_m"]), 1),
                "geo": {"lat": float(row["lat"]), "lon": float(row["lon"])},
                "last_seen": row["last_seen"],
            }
            for row in rows
        ]

    async def nearest(
        self, geo: Geo, *, entity_type: str | None = None, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Nearest entities of a type. The "nearest hospital" query.

        Uses the ``<->`` KNN operator so PostGIS walks the index in distance order rather than sorting
        every candidate — the difference between a bounded query and a full scan once a site has
        millions of rows.
        """
        clauses = ["tenant_id = %s", "geom IS NOT NULL"]
        params: list[Any] = [self.tenant_id]
        if entity_type:
            clauses.append("type = %s")
            params.append(entity_type)
        rows = await self.pool.fetch(
            f"""
            SELECT entity_id, type, label, is_static,
                   ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon,
                   ST_Distance(geom, %s::geography) AS distance_m
              FROM entities
             WHERE {" AND ".join(clauses)}
             ORDER BY geom::geometry <-> %s::geometry
             LIMIT %s
            """,
            (_point(geo), *params, _point(geo), limit),
        )
        return [
            {
                "entity_id": row["entity_id"],
                "type": row["type"],
                "label": row["label"],
                "distance_m": round(float(row["distance_m"]), 1),
                "geo": {"lat": float(row["lat"]), "lon": float(row["lon"])},
            }
            for row in rows
        ]

    async def contains(
        self, zone_id: str, *, active_within_s: float | None = 300.0
    ) -> list[dict[str, Any]]:
        """Entities inside a zone, straight from PostGIS.

        The service also keeps a debounced in-memory view of membership; this is the authoritative
        instantaneous answer, and the infra tests assert the two agree.
        """
        clauses = [
            "e.tenant_id = %s",
            "z.zone_id = %s",
            "e.geom IS NOT NULL",
            "ST_Contains(z.geom::geometry, e.geom::geometry)",
        ]
        params: list[Any] = [self.tenant_id, zone_id]
        if active_within_s is not None:
            clauses.append("(e.is_static OR e.last_seen >= now() - make_interval(secs => %s))")
            params.append(active_within_s)
        rows = await self.pool.fetch(
            f"""
            SELECT e.entity_id, e.type, e.label, e.is_static, e.last_seen
              FROM entities e JOIN zones z ON z.tenant_id = e.tenant_id
             WHERE {" AND ".join(clauses)}
             ORDER BY e.last_seen DESC
            """,
            tuple(params),
        )
        return [dict(row) for row in rows]

    async def zones_at(self, geo: Geo) -> list[dict[str, Any]]:
        """Which zones contain a point, smallest first."""
        rows = await self.pool.fetch(
            """
            SELECT zone_id, name, kind, restricted, ST_Area(geom) AS area_m2
              FROM zones
             WHERE tenant_id = %s AND ST_Contains(geom::geometry, %s::geography::geometry)
             ORDER BY area_m2 ASC
            """,
            (self.tenant_id, _point(geo)),
        )
        return [dict(row) for row in rows]

    async def coverage_of(self, source_id: str) -> dict[str, Any]:
        """Which zones a camera's field of view overlaps, and by how much.

        The inverse — "cameras covering Gate B" — is the same question read the other way, and
        `cameras_covering` below answers it from the same geometry so the two cannot disagree.
        """
        row = await self.pool.fetchrow(
            """
            SELECT source_id, config, ST_AsGeoJSON(fov::geometry) AS fov_geojson
              FROM sources
             WHERE tenant_id = %s AND source_id = %s
            """,
            (self.tenant_id, source_id),
        )
        if row is None:
            return {"source_id": source_id, "found": False, "zones": []}
        zones = await self.pool.fetch(
            """
            SELECT z.zone_id, z.name, z.kind,
                   ST_Area(ST_Intersection(z.geom::geometry, s.fov::geometry)::geography) AS overlap_m2,
                   ST_Area(z.geom) AS zone_m2
              FROM zones z JOIN sources s ON s.tenant_id = z.tenant_id
             WHERE z.tenant_id = %s AND s.source_id = %s AND s.fov IS NOT NULL
               AND ST_Intersects(z.geom::geometry, s.fov::geometry)
             ORDER BY overlap_m2 DESC
            """,
            (self.tenant_id, source_id),
        )
        return {
            "source_id": source_id,
            "found": True,
            "config": row.get("config") or {},
            "zones": [
                {
                    "zone_id": zone["zone_id"],
                    "name": zone["name"],
                    "kind": zone["kind"],
                    "overlap_m2": round(float(zone["overlap_m2"]), 1),
                    "fraction_of_zone": round(
                        float(zone["overlap_m2"]) / max(1.0, float(zone["zone_m2"])), 3
                    ),
                }
                for zone in zones
            ],
        }

    async def cameras_covering(self, zone_id: str) -> list[dict[str, Any]]:
        """Cameras whose field of view overlaps a zone. The "cameras covering Gate B" query."""
        rows = await self.pool.fetch(
            """
            SELECT s.source_id, s.label,
                   ST_Area(ST_Intersection(z.geom::geometry, s.fov::geometry)::geography) AS overlap_m2,
                   ST_Area(z.geom) AS zone_m2
              FROM sources s JOIN zones z ON z.tenant_id = s.tenant_id
             WHERE s.tenant_id = %s AND z.zone_id = %s AND s.kind = 'camera' AND s.fov IS NOT NULL
               AND ST_Intersects(z.geom::geometry, s.fov::geometry)
             ORDER BY overlap_m2 DESC
            """,
            (self.tenant_id, zone_id),
        )
        return [
            {
                "source_id": row["source_id"],
                "name": row["label"],
                "overlap_m2": round(float(row["overlap_m2"]), 1),
                "fraction_of_zone": round(
                    float(row["overlap_m2"]) / max(1.0, float(row["zone_m2"])), 3
                ),
            }
            for row in rows
        ]

    async def blind_spots(self, *, cell_m2: float = 400.0) -> dict[str, Any]:
        """Parts of the site no camera can see.

        Computed as the site's area minus the union of every camera footprint, which is the honest
        definition and something PostGIS does in one statement. Reported both as a fraction and as
        polygons, because "83 per cent covered" tells an operator nothing about *where* to walk.

        This is the query the PRD calls out as a differentiator, and it is only trustworthy because the
        footprints are sectors rather than triangles — a triangle understates the far edge of a wide
        lens by about 8 per cent of its range and would invent gaps that do not exist.
        """
        row = await self.pool.fetchrow(
            """
            WITH site AS (
                SELECT ST_Union(geom::geometry) AS geom FROM zones WHERE tenant_id = %s
            ), seen AS (
                SELECT ST_Union(fov::geometry) AS geom
                  FROM sources
                 WHERE tenant_id = %s AND kind = 'camera' AND fov IS NOT NULL
            )
            SELECT ST_Area(site.geom::geography) AS site_m2,
                   ST_Area(COALESCE(ST_Intersection(site.geom, seen.geom), 'POLYGON EMPTY'::geometry)::geography)
                       AS covered_m2,
                   ST_AsGeoJSON(
                       COALESCE(ST_Difference(site.geom, seen.geom), site.geom)
                   ) AS gaps_geojson
              FROM site, seen
            """,
            (self.tenant_id, self.tenant_id),
        )
        if row is None or row.get("site_m2") is None:
            return {"site_m2": 0.0, "covered_m2": 0.0, "coverage_fraction": 0.0, "gaps": []}
        site_m2 = float(row["site_m2"] or 0.0)
        covered_m2 = float(row["covered_m2"] or 0.0)
        gaps = json.loads(row["gaps_geojson"]) if row.get("gaps_geojson") else None
        return {
            "site_m2": round(site_m2, 1),
            "covered_m2": round(covered_m2, 1),
            "coverage_fraction": round(covered_m2 / site_m2, 3) if site_m2 else 0.0,
            "uncovered_m2": round(max(0.0, site_m2 - covered_m2), 1),
            "gaps": gaps,
            "cell_m2": cell_m2,
        }

    # ------------------------------------------------------------------ H3
    async def h3_density(
        self, *, resolution: int, active_within_s: float = 900.0, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Entity counts per H3 cell — "where in the yard do things actually happen".

        Cells are computed in Python rather than in SQL because Postgres has no H3 extension here, and
        adding one for a bucketing operation would be a heavy dependency for something a hash can do.
        """
        rows = await self.pool.fetch(
            """
            SELECT entity_id, type,
                   ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon
              FROM entities
             WHERE tenant_id = %s AND geom IS NOT NULL AND NOT is_static
               AND last_seen >= now() - make_interval(secs => %s)
             LIMIT %s
            """,
            (self.tenant_id, active_within_s, limit),
        )
        buckets: dict[str, dict[str, Any]] = {}
        for row in rows:
            cell = cell_for(Geo(lat=float(row["lat"]), lon=float(row["lon"])), resolution)
            bucket = buckets.setdefault(cell, {"cell": cell, "count": 0, "types": {}})
            bucket["count"] += 1
            bucket["types"][row["type"]] = bucket["types"].get(row["type"], 0) + 1
        return sorted(buckets.values(), key=lambda item: -item["count"])


def _point(geo: Geo) -> str:
    """A geography literal. Longitude first, which is the order PostGIS expects and the opposite of
    how every human says it."""
    return f"SRID=4326;POINT({geo.lon} {geo.lat})"
