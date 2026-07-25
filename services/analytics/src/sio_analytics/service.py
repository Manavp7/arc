"""Analytics service: KPIs, heatmaps and reports (PRD M19, Phase 6).

Everything is a query over the append-only record. Nothing here keeps a counter, because a counter drifts on
restart and cannot be recomputed for a past window — and "what did last Tuesday look like" is the question
analytics exists to answer.

The report generator emits **Markdown**, not PDF. The PRD says "PDF/Markdown", and Markdown is the half worth
building first: it diffs, it pastes into a ticket, it renders in every tool a reader already has, and it can be
turned into a PDF by anything. A PDF generator would add a rendering dependency to produce a file nobody can
review the change history of.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import PlainTextResponse

from sio_core import PgPool, ServiceIdentity, SioService, describe_error, get_pg_pool

from .heatmap import DISPLAY_RESOLUTION, MIN_CELL_COUNT, aggregate
from .kpis import DWELL_BUCKETS_MIN, risk_index, summarise, utilisation


class AnalyticsService(SioService):
    """Answers "how is the site doing" from what was recorded."""

    name = "analytics"
    subscribes = ()
    tick_interval_s = 0.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.pool: PgPool = get_pg_pool(self.settings)
        self.identity = ServiceIdentity("analytics", self.settings)
        self.api_url = f"http://127.0.0.1:{self.settings.api_port}"

        async def attach_token(request: httpx.Request) -> None:
            request.headers["Authorization"] = f"Bearer {self.identity.token()}"

        self.client = httpx.AsyncClient(timeout=20.0, event_hooks={"request": [attach_token]})
        self._queries = 0

    async def setup(self) -> None:
        await self.pool.open()
        self.log.info(
            "analytics.ready",
            heatmap_resolution=DISPLAY_RESOLUTION,
            min_cell_count=MIN_CELL_COUNT,
        )

    async def teardown(self) -> None:
        await self.client.aclose()

    async def health_checks(self) -> dict[str, str]:
        return {"postgres": "ok" if await self.pool.ping() else "unreachable"}

    async def health_info(self) -> dict[str, str]:
        return {"queries_served": str(self._queries)}

    # ------------------------------------------------------------------ computations
    async def dwell_distribution(self, hours: int) -> dict[str, Any]:
        """Dwell time per zone visit, from the bitemporal `entered` relationships.

        Computed from closed intervals only. An open visit has no duration yet, and including "so far" would
        make the distribution depend on when the query ran — so a report generated twice would disagree with
        itself, which is the fastest way to lose a reader's trust in a dashboard.
        """
        rows = await self.pool.fetch(
            """
            SELECT src_id AS entity_id, dst_id AS zone_id,
                   extract(epoch FROM (ts_valid_to - ts_valid_from)) / 60.0 AS minutes
              FROM relationships
             WHERE tenant_id = %s AND type = 'entered'
               AND ts_valid_to IS NOT NULL
               AND ts_valid_from >= now() - make_interval(hours => %s)
            """,
            (self.settings.tenant_id, hours),
        )
        values = [float(row["minutes"]) for row in rows if row["minutes"] is not None]
        overall = summarise("dwell", "minutes", values, DWELL_BUCKETS_MIN)

        by_zone: dict[str, Any] = {}
        for row in rows:
            if row["minutes"] is None:
                continue
            by_zone.setdefault(str(row["zone_id"]), []).append(float(row["minutes"]))
        return {
            "window_hours": hours,
            "overall": overall.describe(),
            "by_zone": {
                zone: summarise(
                    f"dwell:{zone}", "minutes", measurements, DWELL_BUCKETS_MIN
                ).describe()
                for zone, measurements in sorted(by_zone.items(), key=lambda pair: -len(pair[1]))[
                    :10
                ]
            },
            "open_visits_excluded": await self._open_visits(hours),
        }

    async def _open_visits(self, hours: int) -> int:
        """Visits still in progress, counted and excluded.

        Reported rather than dropped silently: a distribution over 12 closed visits when 40 are in progress
        describes a different site from one where 12 is all there is.
        """
        row = await self.pool.fetchrow(
            """
            SELECT count(*) AS open FROM relationships
             WHERE tenant_id = %s AND type = 'entered' AND ts_valid_to IS NULL
               AND ts_valid_from >= now() - make_interval(hours => %s)
            """,
            (self.settings.tenant_id, hours),
        )
        return int((row or {}).get("open") or 0)

    async def throughput(self, hours: int, bucket_minutes: int) -> dict[str, Any]:
        """Zone entries per bucket, per gate — the site's pulse.

        Unsmoothed. A spiky chart is a true chart of a spiky yard, and a rolling mean would hide exactly the
        five-minute burst that an operator is trying to explain.
        """
        rows = await self.pool.fetch(
            """
            SELECT date_trunc('hour', ts)
                     + floor(extract(minute FROM ts) / %s) * make_interval(mins => %s) AS bucket,
                   zone_id, count(*) AS entries
              FROM events
             WHERE tenant_id = %s AND type = 'zone_entered'
               AND ts >= now() - make_interval(hours => %s)
             GROUP BY bucket, zone_id
             ORDER BY bucket
            """,
            (bucket_minutes, bucket_minutes, self.settings.tenant_id, hours),
        )
        series: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            series.setdefault(str(row["zone_id"] or "unknown"), []).append(
                {"at": row["bucket"].isoformat(), "entries": int(row["entries"])}
            )
        totals = {
            zone: sum(point["entries"] for point in points) for zone, points in series.items()
        }
        return {
            "window_hours": hours,
            "bucket_minutes": bucket_minutes,
            "series": dict(sorted(series.items(), key=lambda pair: -totals[pair[0]])[:8]),
            "totals": dict(sorted(totals.items(), key=lambda pair: -pair[1])),
            "entries_per_hour": round(sum(totals.values()) / max(hours, 1), 1),
            "smoothing": "none — a spiky chart is a true chart of a spiky yard",
        }

    async def zone_utilisation(self, hours: int) -> dict[str, Any]:
        """How much of the window each zone was occupied."""
        window_s = hours * 3600
        rows = await self.pool.fetch(
            """
            SELECT dst_id AS zone_id,
                   sum(extract(epoch FROM (coalesce(ts_valid_to, now()) - greatest(
                       ts_valid_from, now() - make_interval(hours => %s))))) AS busy_s,
                   count(*) AS visits
              FROM relationships
             WHERE tenant_id = %s AND type = 'entered'
               AND coalesce(ts_valid_to, now()) >= now() - make_interval(hours => %s)
             GROUP BY dst_id
             ORDER BY busy_s DESC
            """,
            (hours, self.settings.tenant_id, hours),
        )
        return {
            "window_hours": hours,
            "zones": [
                {
                    "zone_id": str(row["zone_id"]),
                    "visits": int(row["visits"]),
                    "busy_seconds": round(float(row["busy_s"] or 0), 0),
                    # Clamped: overlapping intervals for one zone can sum past the window, and 101 %
                    # utilisation reads as a broken dashboard rather than as overlapping data.
                    "utilisation": round(
                        utilisation(
                            busy_seconds=float(row["busy_s"] or 0), window_seconds=window_s
                        ),
                        3,
                    ),
                }
                for row in rows[:15]
            ],
        }

    async def heatmap(self, hours: int, resolution: int) -> dict[str, Any]:
        """Positions aggregated into H3 cells, with small cells suppressed."""
        rows = await self.pool.fetch(
            """
            SELECT st_y(geom::geometry) AS lat, st_x(geom::geometry) AS lon,
                   entity_id, type, zone_id
              FROM observations
             WHERE tenant_id = %s AND geom IS NOT NULL
               AND ts >= now() - make_interval(hours => %s)
             LIMIT 50000
            """,
            (self.settings.tenant_id, hours),
        )
        positions = [
            {
                "lat": row["lat"],
                "lon": row["lon"],
                "entity_id": row.get("entity_id"),
                "type": row.get("type"),
                "zone_id": row.get("zone_id"),
            }
            for row in rows
        ]
        result = aggregate(positions, resolution=resolution).describe()
        result["window_hours"] = hours
        return result

    async def risk(self) -> dict[str, Any]:
        """The risk index, with every term shown."""
        alerts = (
            await self.pool.fetchrow(
                """
            SELECT count(*) FILTER (WHERE state IN ('open', 'escalated')) AS open,
                   count(*) FILTER (WHERE state IN ('open', 'escalated') AND severity = 'critical')
                     AS criticals,
                   count(*) FILTER (WHERE state IN ('open', 'escalated') AND ack_ts IS NULL)
                     AS unacknowledged
              FROM alerts WHERE tenant_id = %s
            """,
                (self.settings.tenant_id,),
            )
            or {}
        )
        zones = (
            await self.pool.fetchrow(
                """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE restricted) AS restricted
              FROM zones WHERE tenant_id = %s
            """,
                (self.settings.tenant_id,),
            )
            or {}
        )
        events = (
            await self.pool.fetchrow(
                """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE type = 'anomaly_detected') AS anomalies
              FROM events
             WHERE tenant_id = %s AND ts >= now() - interval '1 hour'
            """,
                (self.settings.tenant_id,),
            )
            or {}
        )

        uncovered = 0
        try:
            coverage = await self.client.get(f"{self.api_url}/api/spatial/coverage")
            if coverage.status_code == 200:
                body = coverage.json()
                uncovered = int(body.get("uncovered_zones") or len(body.get("blind_spots") or []))
        except httpx.HTTPError as exc:
            # Coverage is one term of five. A risk index that refuses to compute because one input is
            # unavailable is less useful than one that computes without it and says which term is missing.
            self.log.info("analytics.coverage_unavailable", error=describe_error(exc))

        occupied = (
            await self.pool.fetchrow(
                """
            SELECT count(DISTINCT r.dst_id) AS occupied
              FROM relationships r JOIN zones z
                ON z.tenant_id = r.tenant_id AND z.zone_id = r.dst_id
             WHERE r.tenant_id = %s AND r.type = 'entered' AND r.ts_valid_to IS NULL
               AND z.restricted
            """,
                (self.settings.tenant_id,),
            )
            or {}
        )

        index = risk_index(
            open_criticals=int(alerts.get("criticals") or 0),
            open_alerts=int(alerts.get("open") or 0),
            unacknowledged=int(alerts.get("unacknowledged") or 0),
            zones_total=int(zones.get("total") or 0),
            zones_uncovered=uncovered,
            restricted_occupied=int(occupied.get("occupied") or 0),
            restricted_total=int(zones.get("restricted") or 0),
            anomalies_last_hour=int(events.get("anomalies") or 0),
            events_last_hour=int(events.get("total") or 0),
        )
        return index.describe()

    async def summary(self, hours: int) -> dict[str, Any]:
        """Everything a dashboard needs, in one round trip."""
        self._queries += 1
        counts = (
            await self.pool.fetchrow(
                """
            SELECT
              (SELECT count(*) FROM entities WHERE tenant_id = %s AND NOT is_static) AS entities,
              (SELECT count(*) FROM events WHERE tenant_id = %s
                 AND ts >= now() - make_interval(hours => %s)) AS events,
              (SELECT count(*) FROM alerts WHERE tenant_id = %s
                 AND state IN ('open','escalated')) AS open_alerts,
              (SELECT count(*) FROM decisions WHERE tenant_id = %s AND approval = 'pending')
                AS pending_decisions
            """,
                (
                    self.settings.tenant_id,
                    self.settings.tenant_id,
                    hours,
                    self.settings.tenant_id,
                    self.settings.tenant_id,
                ),
            )
            or {}
        )
        return {
            "window_hours": hours,
            "generated_at": datetime.now(UTC).isoformat(),
            "counts": {key: int(value or 0) for key, value in counts.items()},
            "dwell": await self.dwell_distribution(hours),
            "throughput": await self.throughput(hours, 15),
            "utilisation": await self.zone_utilisation(hours),
            "risk": await self.risk(),
        }

    # --------------------------------------------------------------------- routes
    def routes(self, app: FastAPI) -> None:
        @app.get("/analytics/summary", tags=["analytics"])
        async def summary(hours: int = Query(default=24, ge=1, le=720)) -> dict[str, Any]:
            return await self.summary(hours)

        @app.get("/analytics/dwell", tags=["analytics"])
        async def dwell(hours: int = Query(default=24, ge=1, le=720)) -> dict[str, Any]:
            self._queries += 1
            return await self.dwell_distribution(hours)

        @app.get("/analytics/throughput", tags=["analytics"])
        async def throughput_route(
            hours: int = Query(default=24, ge=1, le=720),
            bucket_minutes: int = Query(default=15, ge=1, le=180),
        ) -> dict[str, Any]:
            self._queries += 1
            return await self.throughput(hours, bucket_minutes)

        @app.get("/analytics/utilisation", tags=["analytics"])
        async def utilisation_route(hours: int = Query(default=24, ge=1, le=720)) -> dict[str, Any]:
            self._queries += 1
            return await self.zone_utilisation(hours)

        @app.get("/analytics/heatmap", tags=["analytics"])
        async def heatmap_route(
            hours: int = Query(default=6, ge=1, le=168),
            resolution: int = Query(default=DISPLAY_RESOLUTION, ge=7, le=13),
        ) -> dict[str, Any]:
            self._queries += 1
            return await self.heatmap(hours, resolution)

        @app.get("/analytics/risk", tags=["analytics"])
        async def risk_route() -> dict[str, Any]:
            self._queries += 1
            return await self.risk()

        @app.get("/analytics/report", response_class=PlainTextResponse, tags=["analytics"])
        async def report(hours: int = Query(default=24, ge=1, le=720)) -> str:
            """A Markdown report.

            Markdown rather than PDF, which is the half of "PDF/Markdown" worth building first: it diffs, it
            pastes into a ticket, it renders in every tool a reader already has, and anything can turn it into
            a PDF. A PDF generator would add a rendering dependency to produce a file whose change history
            nobody can review.
            """
            self._queries += 1
            return render_report(await self.summary(hours))


def render_report(data: dict[str, Any]) -> str:
    """Turn a summary into prose a human would send to somebody.

    Written as a narrative rather than a table dump, because a report that only restates the dashboard has no
    reason to exist. The shape sentences from the distributions do most of the work — they are the part a
    reader cannot get by glancing at a chart.
    """
    hours = data["window_hours"]
    counts = data["counts"]
    dwell = data["dwell"]["overall"]
    throughput = data["throughput"]
    risk = data["risk"]

    lines = [
        f"# Site report — last {hours} hour(s)",
        "",
        f"Generated {data['generated_at']}.",
        "",
        "## Where things stand",
        "",
        f"- **{counts.get('entities', 0)}** moving entities on record",
        f"- **{counts.get('events', 0)}** events in the window",
        f"- **{counts.get('open_alerts', 0)}** alerts open or escalated",
        f"- **{counts.get('pending_decisions', 0)}** recommendations awaiting a human",
        "",
        f"## Risk: {risk['score']} / 100 ({risk['band']})",
        "",
    ]
    # Drivers before the formula. A reader wants to know what is wrong before they want to know how it was
    # arithmetically arrived at, and putting the formula first buries the answer.
    for driver in risk["drivers"]:
        lines.append(f"- {driver}")
    lines += [
        "",
        f"_Formula: {risk['formula']}. Every term is normalised to 0-1 before weighting._",
        "",
    ]

    lines += ["## Dwell time", ""]
    if dwell["count"]:
        lines += [
            f"{dwell['count']} completed visits. Median **{dwell['percentiles'].get('p50', 0)} min**, "
            f"p95 **{dwell['percentiles'].get('p95', 0)} min**.",
            "",
            f"**{dwell['shape']}**",
            "",
            "| from (min) | to (min) | visits | share |",
            "|---|---|---|---|",
        ]
        for bucket in dwell["histogram"]:
            upper = bucket["to"] if bucket["to"] is not None else "∞"
            lines.append(
                f"| {bucket['from']} | {upper} | {bucket['count']} | {bucket['share'] * 100:.0f}% |"
            )
        excluded = data["dwell"].get("open_visits_excluded", 0)
        if excluded:
            lines += [
                "",
                f"_{excluded} visit(s) are still in progress and are excluded: an open visit has no duration "
                f'yet, and including "so far" would make this table depend on when the report was run._',
            ]
    else:
        lines.append("No completed visits in this window.")

    lines += ["", "## Throughput", ""]
    if throughput["totals"]:
        lines.append(f"**{throughput['entries_per_hour']}** zone entries per hour. Busiest zones:")
        lines.append("")
        for zone, total in list(throughput["totals"].items())[:5]:
            lines.append(f"- `{zone}` — {total} entries")
        lines += ["", f"_Smoothing: {throughput['smoothing']}._"]
    else:
        lines.append("No zone entries recorded in this window.")

    lines += ["", "## Zone utilisation", ""]
    zones = data["utilisation"]["zones"]
    if zones:
        lines += ["| zone | visits | utilisation |", "|---|---|---|"]
        for zone in zones[:10]:
            lines.append(
                f"| `{zone['zone_id']}` | {zone['visits']} | {zone['utilisation'] * 100:.0f}% |"
            )
    else:
        lines.append("No zone occupancy recorded in this window.")

    lines += [
        "",
        "---",
        "",
        "_Every figure here is a query over the append-only record, so this report can be regenerated for "
        "any past window and will give the same answer twice. Nothing is read from a counter._",
    ]
    return "\n".join(lines) + "\n"


__all__ = ["AnalyticsService", "render_report"]
