#!/usr/bin/env python3
"""Seed the demo site: zones, sources and the GeoJSON export.

    uv run python scripts/seed.py            # load the site into Postgres
    uv run python scripts/seed.py --geojson  # also write infra/site/yard.geojson
    uv run python scripts/seed.py --clear    # remove seeded rows first

The moving cast is produced by the ingest service at runtime; what this script loads is the *fixed*
world: zone polygons, camera positions and fields of view, sensor locations. Those have to be in
PostGIS before spatial questions can be answered ("which cameras cover Gate B", "is this entity
inside a restricted zone"), and they are what the map draws as context.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "libs" / "sio_core" / "src"))
sys.path.insert(0, str(REPO_ROOT / "libs" / "sio_schemas" / "src"))
sys.path.insert(0, str(REPO_ROOT / "services" / "ingest" / "src"))


def polygon_wkt(corners: list[tuple[float, float]]) -> str:
    from sio_ingest.site import to_geo

    points = [to_geo(east, north) for east, north in corners]
    ring = ", ".join(f"{p.lon} {p.lat}" for p in points)
    first = points[0]
    return f"POLYGON(({ring}, {first.lon} {first.lat}))"


async def seed(*, clear: bool, write_geojson: bool) -> int:
    from sio_ingest.site import load_site

    from sio_core.config import get_settings
    from sio_core.stores.pg import PgPool

    cfg = get_settings()
    site = load_site(cfg.sim_site)
    pool = PgPool(cfg.pg_dsn, min_size=1, max_size=2)

    if not await pool.ping():
        print(f"postgres unreachable at {cfg.pg_host}:{cfg.pg_port}", file=sys.stderr)
        print("start it with:  just services", file=sys.stderr)
        return 2

    tenant = cfg.tenant_id
    print(f"seeding '{site.name}' into {cfg.pg_database} (tenant: {tenant})")

    if clear:
        for table in ("zones", "sources"):
            removed = await pool.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant,))
            print(f"  cleared {removed} rows from {table}")

    # ---- zones -------------------------------------------------------------
    for zone in site.zones:
        await pool.execute(
            """
            INSERT INTO zones (tenant_id, zone_id, name, kind, geom, restricted, capacity, attributes)
            VALUES (%s, %s, %s, %s, ST_SetSRID(ST_GeomFromText(%s), 4326)::geography, %s, %s, %s::jsonb)
            ON CONFLICT (tenant_id, zone_id) DO UPDATE SET
                name = EXCLUDED.name, kind = EXCLUDED.kind, geom = EXCLUDED.geom,
                restricted = EXCLUDED.restricted, capacity = EXCLUDED.capacity
            """,
            (
                tenant,
                zone.zone_id,
                zone.name,
                zone.kind,
                polygon_wkt(list(zone.corners)),
                zone.restricted,
                zone.capacity,
                json.dumps({}),
            ),
        )
    print(f"  {len(site.zones)} zones ({sum(1 for z in site.zones if z.restricted)} restricted)")

    # ---- cameras -----------------------------------------------------------
    for camera in site.cameras:
        fov = camera.fov_polygon()
        ring = ", ".join(f"{lon} {lat}" for lon, lat in fov["coordinates"][0])
        await pool.execute(
            """
            INSERT INTO sources (tenant_id, source_id, kind, modality, label, geom, fov, zone_id, config)
            VALUES (
                %s, %s, 'camera', 'video', %s,
                ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                ST_SetSRID(ST_GeomFromText(%s), 4326)::geography,
                %s, %s::jsonb
            )
            ON CONFLICT (tenant_id, source_id) DO UPDATE SET
                label = EXCLUDED.label, geom = EXCLUDED.geom, fov = EXCLUDED.fov,
                config = EXCLUDED.config
            """,
            (
                tenant,
                camera.source_id,
                camera.label,
                camera.geo.lon,
                camera.geo.lat,
                f"POLYGON(({ring}))",
                camera.covers[0] if camera.covers else None,
                json.dumps(
                    {
                        "bearing_deg": camera.bearing_deg,
                        "fov_deg": camera.fov_deg,
                        "range_m": camera.range_m,
                        "covers": list(camera.covers),
                    }
                ),
            ),
        )
    print(f"  {len(site.cameras)} cameras with fields of view")

    # ---- fixed sensors -----------------------------------------------------
    for sensor in site.sensors:
        await pool.execute(
            """
            INSERT INTO sources (tenant_id, source_id, kind, modality, label, geom, zone_id, config)
            VALUES (
                %s, %s, %s, 'iot', %s,
                ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, %s::jsonb
            )
            ON CONFLICT (tenant_id, source_id) DO UPDATE SET
                label = EXCLUDED.label, geom = EXCLUDED.geom, config = EXCLUDED.config
            """,
            (
                tenant,
                sensor.source_id,
                "rfid" if sensor.metric == "rfid_read" else "iot",
                sensor.label,
                sensor.geo.lon,
                sensor.geo.lat,
                sensor.zone_id,
                json.dumps(
                    {"metric": sensor.metric, "unit": sensor.unit, "baseline": sensor.baseline}
                ),
            ),
        )
    print(f"  {len(site.sensors)} fixed sensors")

    # ---- verify with the query the platform will actually run --------------
    covering = await pool.fetch(
        """
        SELECT s.source_id FROM sources s
        JOIN zones z ON z.tenant_id = s.tenant_id AND z.zone_id = 'gate_a'
        WHERE s.tenant_id = %s AND s.kind = 'camera' AND s.fov IS NOT NULL
          AND ST_Intersects(s.fov, z.geom)
        """,
        (tenant,),
    )
    print(
        f"  spatial check — cameras covering gate_a: {[r['source_id'] for r in covering] or 'none'}"
    )

    if write_geojson:
        path = site.write_geojson(REPO_ROOT / cfg.sim_site)
        size_kb = path.stat().st_size / 1024
        print(f"  wrote {path.relative_to(REPO_ROOT)} ({size_kb:.0f} kB)")

    await pool.close()
    print("\nseeded. start the platform with:  just dev")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the SIO demo site")
    parser.add_argument("--clear", action="store_true", help="delete existing zones/sources first")
    parser.add_argument("--geojson", action="store_true", help="also write the GeoJSON export")
    args = parser.parse_args(argv)
    return asyncio.run(seed(clear=args.clear, write_geojson=args.geojson))


if __name__ == "__main__":
    raise SystemExit(main())
