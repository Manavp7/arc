"""Prediction service: forecasts with intervals, trajectories with cones (PRD M10).

Reads history from Postgres rather than accumulating it in memory. Forecasting needs an hour of past,
and a service that rebuilt that from the bus on every restart would be blind for an hour after every
deploy — which is exactly when someone is watching.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from sio_core import MessageContext, PgPool, SioService, get_pg_pool
from sio_schemas import BusMessage, Entity, Forecast, Geo, Topic, utc_now

from .forecasters import backtest
from .series import GapPolicy, Series, bucketise, counts_per_bucket
from .targets import (
    SPECS,
    TargetForecast,
    build,
    congestion_from_occupancy,
    time_to_threshold,
)
from .trajectory import (
    Kinematics,
    Trajectory,
    predict_next_zones,
    predict_trajectory,
    turn_rate_from_headings,
)

BATTERY_RESERVE_PCT = 25.0
"""Battery level a drone must not go below in flight.

A reserve, not the point of exhaustion: the useful prediction is when to turn back, and that has to
account for the flight home. Predicting the moment of zero would be technically accurate and
operationally useless.
"""


class PredictionService(SioService):
    """Answers what is about to happen, with intervals that have been checked."""

    name = "prediction"
    subscribes = (Topic.ENTITIES,)
    tick_interval_s = 60.0

    HEADING_HISTORY = 6
    """Recent headings kept per entity, for estimating a turn rate."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.pool: PgPool = get_pg_pool(self.settings)
        self._kinematics: dict[str, Kinematics] = {}
        self._headings: dict[str, list[tuple[datetime, float]]] = {}
        self._zones: dict[str, dict[str, Any]] = {}
        self._latest: dict[str, Forecast] = {}
        self._published = 0
        self._skipped_short_history = 0
        self._last_cycle_s = 0.0

    async def setup(self) -> None:
        await self.pool.open()
        await self._load_zones()
        self.log.info(
            "prediction.ready",
            zones=len(self._zones),
            targets=sorted({spec.target for spec in SPECS.values()}),
            horizon_s=self.settings.forecast_horizon_s,
        )

    async def _load_zones(self) -> None:
        rows = await self.pool.fetch(
            "SELECT zone_id, name, capacity, kind FROM zones WHERE tenant_id = %s",
            (self.settings.tenant_id,),
        )
        self._zones = {str(row["zone_id"]): dict(row) for row in rows}

    async def health_checks(self) -> dict[str, str]:
        return {
            "postgres": "ok" if await self.pool.ping() else "unreachable",
            "zones": f"ok ({len(self._zones)} zones)"
            if self._zones
            else "no zones (run: just seed)",
        }

    async def health_info(self) -> dict[str, str]:
        return {
            "forecasts_published": str(self._published),
            "tracked_entities": str(len(self._kinematics)),
            "skipped_short_history": str(self._skipped_short_history),
            "last_cycle_s": f"{self._last_cycle_s:.2f}",
            "latest_targets": ",".join(sorted(self._latest)) or "none",
        }

    # ------------------------------------------------------------------ handling
    async def on_message(self, message: BusMessage, ctx: MessageContext) -> None:
        """Keep the freshest kinematics per entity, for trajectory queries.

        Only the latest state is retained. Trajectory prediction is a question asked *now* about a
        specific entity, so keeping a history of everything would be a memory leak in service of
        nothing — the exception being headings, where a short window is needed to see a turn.
        """
        if message.kind != "Entity":
            return
        entity = message.decode(Entity)
        state = entity.state
        if entity.is_static or state is None or state.geo is None:
            return

        speed = state.velocity.speed_mps if state.velocity else 0.0
        headings = self._headings.setdefault(entity.entity_id, [])
        if state.heading_deg is not None:
            headings.append((state.ts, state.heading_deg))
            del headings[: max(0, len(headings) - self.HEADING_HISTORY)]

        self._kinematics[entity.entity_id] = Kinematics(
            geo=state.geo,
            speed_mps=speed,
            heading_deg=state.heading_deg,
            ts=state.ts,
            turn_rate_deg_s=turn_rate_from_headings(headings),
            position_sigma_m=float(entity.attributes.get("position_sigma_m") or 3.0),
        )

    # ----------------------------------------------------------------- forecasts
    async def tick(self) -> None:
        started = time.perf_counter()
        made_at = utc_now()
        produced: list[Forecast] = []

        for forecast in await self._forecast_all(made_at):
            await self._persist(forecast)
            await self.publish(Topic.FORECASTS, forecast)
            self._latest[self._latest_key(forecast)] = forecast
            self._published += 1
            produced.append(forecast)

        self._prune_kinematics(made_at)
        self._last_cycle_s = time.perf_counter() - started
        self.log.info(
            "prediction.cycle",
            forecasts=len(produced),
            targets=sorted({forecast.target for forecast in produced}),
            skipped=self._skipped_short_history,
            seconds=round(self._last_cycle_s, 2),
        )

    async def _forecast_all(self, made_at: datetime) -> list[Forecast]:
        forecasts: list[Forecast] = []
        for target in await self._site_forecasts(made_at):
            forecasts.append(target)
        for target in await self._zone_forecasts(made_at):
            forecasts.append(target)
        for target in await self._sensor_forecasts(made_at):
            forecasts.append(target)
        return forecasts

    async def _site_forecasts(self, made_at: datetime) -> list[Forecast]:
        """Throughput: how many entities are entering the site per minute."""
        spec = SPECS["throughput"]
        rows = await self.pool.fetch(
            """
            SELECT ts FROM events
             WHERE tenant_id = %s AND type = 'zone_entered'
               AND zone_id LIKE 'gate%%'
               AND ts >= now() - make_interval(secs => %s)
             ORDER BY ts
            """,
            (self.settings.tenant_id, spec.lookback_s),
        )
        series = counts_per_bucket(
            [row["ts"] for row in rows],
            name="throughput",
            bucket_s=spec.bucket_s,
            now=made_at,
            lookback_s=spec.lookback_s,
        )
        if series is None or len(series) < 5:
            self._skipped_short_history += 1
            return []
        target = build(spec, series, level=self.settings.forecast_interval_level)
        return (
            [target.to_forecast(self.settings.tenant_id, made_at=made_at)] if target.points else []
        )

    async def _zone_forecasts(self, made_at: datetime) -> list[Forecast]:
        """Occupancy per zone, and the congestion statement that follows from it."""
        spec = SPECS["occupancy"]
        forecasts: list[Forecast] = []
        rows = await self.pool.fetch(
            """
            SELECT zone_id, ts, entity_id FROM events
             WHERE tenant_id = %s AND type IN ('zone_entered', 'zone_exited')
               AND ts >= now() - make_interval(secs => %s)
             ORDER BY ts
            """,
            (self.settings.tenant_id, spec.lookback_s),
        )
        by_zone: dict[str, list[tuple[datetime, float]]] = {}
        running: dict[str, set[str]] = {}
        for row in rows:
            zone = str(row["zone_id"] or "")
            if not zone:
                continue
            occupants = running.setdefault(zone, set())
            entity = str(row["entity_id"] or "")
            # Reconstruct occupancy by replaying entries and exits. The alternative — sampling
            # `entities.zone_id` — only ever shows the present, and a forecast needs the past.
            occupants.add(entity)
            by_zone.setdefault(zone, []).append((row["ts"], float(len(occupants))))

        for zone_id, samples in by_zone.items():
            series = bucketise(
                samples,
                name=f"occupancy:{zone_id}",
                bucket_s=spec.bucket_s,
                now=made_at,
                policy=spec.policy,
                aggregate=spec.aggregate,
                lookback_s=spec.lookback_s,
                unit=spec.unit,
            )
            if series is None or len(series) < 5:
                self._skipped_short_history += 1
                continue
            target = build(
                spec, series, level=self.settings.forecast_interval_level, zone_id=zone_id
            )
            if not target.points:
                continue
            forecast = target.to_forecast(self.settings.tenant_id, made_at=made_at)
            congestion = congestion_from_occupancy(
                target, capacity=self._zones.get(zone_id, {}).get("capacity")
            )
            if congestion:
                forecast.explanation.notes.append(
                    f"capacity {congestion['capacity']}, predicted peak {congestion['predicted_peak']}"
                    + (
                        f", may exceed in {congestion['eta_s']:.0f}s"
                        if congestion["will_exceed"]
                        else f", headroom {congestion['headroom']}"
                    )
                )
                forecast.explanation.notes.append(
                    "read against the upper bound of the interval, not the central estimate: the useful "
                    "question is whether it MIGHT overflow"
                )
            forecasts.append(forecast)
        return forecasts

    async def _sensor_forecasts(self, made_at: datetime) -> list[Forecast]:
        """Temperature, battery and vibration, straight from the measurements table."""
        forecasts: list[Forecast] = []
        wanted = {
            "temperature_c": SPECS["temperature"],
            "battery_pct": SPECS["battery"],
            "vibration_mm_s": SPECS["vibration"],
        }
        for metric, spec in wanted.items():
            rows = await self.pool.fetch(
                """
                SELECT source_id, zone_id, ts, value FROM measurements
                 WHERE tenant_id = %s AND metric = %s
                   AND ts >= now() - make_interval(secs => %s)
                 ORDER BY ts
                """,
                (self.settings.tenant_id, metric, spec.lookback_s),
            )
            by_source: dict[str, list[tuple[datetime, float]]] = {}
            zones: dict[str, str | None] = {}
            for row in rows:
                source = str(row["source_id"])
                by_source.setdefault(source, []).append((row["ts"], float(row["value"])))
                zones[source] = row["zone_id"]

            for source_id, samples in by_source.items():
                series = bucketise(
                    samples,
                    name=f"{metric}:{source_id}",
                    bucket_s=spec.bucket_s,
                    now=made_at,
                    policy=spec.policy,
                    aggregate=spec.aggregate,
                    lookback_s=spec.lookback_s,
                    unit=spec.unit,
                )
                if series is None or len(series) < 5:
                    self._skipped_short_history += 1
                    continue
                target = build(
                    spec,
                    series,
                    level=self.settings.forecast_interval_level,
                    zone_id=zones.get(source_id),
                    entity_id=source_id if metric == "battery_pct" else None,
                )
                if not target.points:
                    continue
                forecast = target.to_forecast(self.settings.tenant_id, made_at=made_at)
                if metric == "battery_pct":
                    seconds = time_to_threshold(
                        target.points, threshold=BATTERY_RESERVE_PCT, falling=True
                    )
                    forecast.explanation.notes.append(
                        f"reserve of {BATTERY_RESERVE_PCT:.0f}% may be reached in {seconds:.0f}s"
                        if seconds is not None
                        else f"reserve of {BATTERY_RESERVE_PCT:.0f}% not reached within the horizon"
                    )
                    forecast.explanation.notes.append(
                        "measured against the LOWER bound: a drone should turn back when it might hit "
                        "the reserve, not when its average estimate does"
                    )
                forecasts.append(forecast)
        return forecasts

    async def _persist(self, forecast: Forecast) -> None:
        await self.pool.execute(
            """
            INSERT INTO forecasts (
                tenant_id, forecast_id, target, entity_id, zone_id, ts, horizon_s,
                model_name, confidence, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (tenant_id, forecast_id) DO NOTHING
            """,
            (
                forecast.tenant_id,
                forecast.forecast_id,
                forecast.target,
                forecast.entity_id,
                forecast.zone_id,
                forecast.ts,
                forecast.horizon_s,
                forecast.model_name,
                forecast.confidence,
                forecast.to_json(),
            ),
        )

    def _prune_kinematics(self, now: datetime) -> None:
        """Forget entities nothing has reported recently.

        Without this the service accumulates one record per entity id ever seen, and ids are minted for
        every new object on site.
        """
        cutoff = now - timedelta(seconds=self.settings.fusion_max_stale_s)
        for entity_id, kinematics in list(self._kinematics.items()):
            if kinematics.ts < cutoff:
                del self._kinematics[entity_id]
                self._headings.pop(entity_id, None)

    @staticmethod
    def _latest_key(forecast: Forecast) -> str:
        scope = forecast.zone_id or forecast.entity_id or "site"
        return f"{forecast.target}:{scope}"

    # -------------------------------------------------------------------- routes
    def routes(self, app: FastAPI) -> None:
        @app.get("/forecasts", tags=["prediction"])
        async def forecasts(
            target: str | None = None, limit: int = Query(20, ge=1, le=200)
        ) -> dict[str, Any]:
            """Latest forecasts with their intervals and the evidence behind them."""
            rows = await self.pool.fetch(
                """
                SELECT payload FROM forecasts
                 WHERE tenant_id = %s AND (%s IS NULL OR target = %s)
                 ORDER BY ts DESC LIMIT %s
                """,
                (self.settings.tenant_id, target, target, limit),
            )
            return {"forecasts": [row["payload"] for row in rows]}

        @app.get("/forecasts/latest", tags=["prediction"])
        async def latest() -> dict[str, Any]:
            """One forecast per target and scope, as this process last computed it."""
            return {
                "forecasts": {
                    key: {
                        "target": forecast.target,
                        "zone_id": forecast.zone_id,
                        "entity_id": forecast.entity_id,
                        "model": forecast.model_name,
                        "confidence": forecast.confidence,
                        "interval_level": forecast.interval_level,
                        "horizon_s": forecast.horizon_s,
                        "summary": forecast.explanation.summary,
                        "why": forecast.explanation.notes,
                        "points": [point.to_wire() for point in forecast.points],
                    }
                    for key, forecast in sorted(self._latest.items())
                }
            }

        @app.post("/forecasts/run", tags=["prediction"])
        async def run_now() -> dict[str, Any]:
            """Force a forecasting cycle, rather than waiting for the timer."""
            await self.tick()
            return {"published": self._published, "cycle_s": round(self._last_cycle_s, 3)}

        @app.get("/predict/trajectory/{entity_id}", tags=["prediction"])
        async def trajectory(
            entity_id: str,
            horizon_s: float = Query(60.0, gt=0, le=600),
            step_s: float = Query(5.0, gt=0, le=60),
        ) -> dict[str, Any]:
            """Where an entity is heading, with its uncertainty cone and likely next zones."""
            kinematics = self._kinematics.get(entity_id)
            if kinematics is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"no recent kinematics for {entity_id}; it may have left or never moved",
                )
            path = predict_trajectory(entity_id, kinematics, horizon_s=horizon_s, step_s=step_s)
            zones = await self._next_zones(path, kinematics)
            return {
                "entity_id": entity_id,
                "model": path.model_name,
                "stationary": path.stationary,
                "confidence": path.confidence(),
                "horizon_s": path.horizon_s,
                "final_sigma_m": round(path.final_sigma_m, 2),
                "why": path.notes,
                "points": [point.to_dict() for point in path.points],
                "next_zones": [zone.to_dict() for zone in zones],
            }

        @app.get("/predict/backtest", tags=["prediction"])
        async def backtest_route(
            metric: str = "temperature_c",
            source_id: str | None = None,
            level: float = Query(0.9, gt=0, lt=1),
        ) -> dict[str, Any]:
            """Measure whether the intervals are honest on real history.

            The endpoint that makes every confidence number in this service checkable: hold out the
            tail, forecast it, and count how often the truth landed inside the interval.
            """
            spec = next(
                (
                    candidate
                    for key, candidate in SPECS.items()
                    if key in metric or candidate.target in metric
                ),
                SPECS["temperature"],
            )
            rows = await self.pool.fetch(
                """
                SELECT source_id, ts, value FROM measurements
                 WHERE tenant_id = %s AND metric = %s AND (%s IS NULL OR source_id = %s)
                   AND ts >= now() - make_interval(secs => %s)
                 ORDER BY ts
                """,
                (self.settings.tenant_id, metric, source_id, source_id, spec.lookback_s * 2),
            )
            if not rows:
                raise HTTPException(
                    status_code=404, detail=f"no measurements for metric {metric!r}"
                )
            by_source: dict[str, list[tuple[datetime, float]]] = {}
            for row in rows:
                by_source.setdefault(str(row["source_id"]), []).append(
                    (row["ts"], float(row["value"]))
                )

            results = []
            for source, samples in by_source.items():
                series = bucketise(
                    samples,
                    name=f"{metric}:{source}",
                    bucket_s=spec.bucket_s,
                    now=utc_now(),
                    policy=spec.policy,
                    aggregate=spec.aggregate,
                    unit=spec.unit,
                )
                if series is None:
                    continue
                measured = backtest(
                    series, horizon=5, level=level, season_length=spec.season_buckets
                )
                results.append(
                    {
                        "source_id": source,
                        "series": series.describe(),
                        "backtest": measured.describe() if measured else None,
                    }
                )
            return {"metric": metric, "level": level, "results": results}

        @app.get("/predict/series", tags=["prediction"])
        async def series_debug(metric: str = "temperature_c") -> dict[str, Any]:
            """The resampled series a forecast was built from.

            Exposed because resampling decisions — bucket size, gap policy, the dropped trailing
            bucket — do more damage than model choice, and they are invisible in a forecast.
            """
            spec = next(
                (candidate for key, candidate in SPECS.items() if key in metric),
                SPECS["temperature"],
            )
            rows = await self.pool.fetch(
                """
                SELECT source_id, ts, value FROM measurements
                 WHERE tenant_id = %s AND metric = %s AND ts >= now() - make_interval(secs => %s)
                 ORDER BY ts
                """,
                (self.settings.tenant_id, metric, spec.lookback_s),
            )
            grouped: dict[str, list[tuple[datetime, float]]] = {}
            for row in rows:
                grouped.setdefault(str(row["source_id"]), []).append(
                    (row["ts"], float(row["value"]))
                )
            described = []
            for source, samples in grouped.items():
                series = bucketise(
                    samples,
                    name=f"{metric}:{source}",
                    bucket_s=spec.bucket_s,
                    now=utc_now(),
                    policy=spec.policy,
                    aggregate=spec.aggregate,
                    lookback_s=spec.lookback_s,
                )
                if series is not None:
                    described.append({**series.describe(), "policy": str(spec.policy)})
            return {"metric": metric, "bucket_s": spec.bucket_s, "series": described}

    async def _next_zones(self, path: Trajectory, kinematics: Kinematics) -> list[Any]:
        """Which zones the cone is likely to enter.

        Point-in-polygon is asked of PostGIS rather than reimplemented: the spatial service owns that
        question, and a second implementation here would eventually disagree with it.
        """
        if not path.points or path.stationary:
            return []
        current = await self.pool.fetch(
            """
            SELECT zone_id FROM zones
             WHERE tenant_id = %s
               AND ST_Contains(geom::geometry, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
            """,
            (self.settings.tenant_id, kinematics.geo.lon, kinematics.geo.lat),
        )
        current_zones = tuple(str(row["zone_id"]) for row in current)

        cache: dict[tuple[float, float], list[str]] = {}

        def contains(geo: Geo) -> list[str]:
            # Rounded to about a metre so nearby probes share a lookup. Sixty sampled paths times a
            # dozen steps is 720 probes per query, and a database round trip each would make this
            # endpoint unusable.
            key = (round(geo.lat, 5), round(geo.lon, 5))
            if key not in cache:
                cache[key] = self._zones_at_sync(geo)
            return cache[key]

        # Load the polygons once and test locally, using the same shapely path the spatial service uses.
        await self._ensure_polygons()
        return predict_next_zones(path, contains, current_zones=current_zones)

    async def _ensure_polygons(self) -> None:
        """Load zone polygons for local point-in-polygon tests.

        Uses the spatial service's own `ZoneIndex` when it is importable, so there is one implementation
        of the geometry rather than a second that drifts. Falls back to skipping next-zone prediction
        rather than writing a competing one.
        """
        if getattr(self, "_zone_index", None) is not None:
            return
        try:
            from sio_spatial.geometry import ZoneIndex, zone_shape_from_row
        except ImportError:
            self._zone_index = None  # type: ignore[attr-defined]
            self.log.warning(
                "prediction.no_zone_index",
                effect="next-zone prediction unavailable",
                reason="sio_spatial not importable",
            )
            return
        import json

        rows = await self.pool.fetch(
            """
            SELECT zone_id, name, kind, restricted, capacity, attributes,
                   ST_AsGeoJSON(geom::geometry) AS geojson
              FROM zones WHERE tenant_id = %s
            """,
            (self.settings.tenant_id,),
        )
        shapes = []
        for row in rows:
            record = dict(row)
            if isinstance(record.get("geojson"), str):
                record["geojson"] = json.loads(record["geojson"])
            shape = zone_shape_from_row(record)
            if shape is not None:
                shapes.append(shape)
        self._zone_index = ZoneIndex(shapes)  # type: ignore[attr-defined]

    def _zones_at_sync(self, geo: Geo) -> list[str]:
        index = getattr(self, "_zone_index", None)
        if index is None:
            return []
        return [zone.zone_id for zone in index.zones_containing(geo)]


__all__ = ["GapPolicy", "PredictionService", "Series", "TargetForecast"]
