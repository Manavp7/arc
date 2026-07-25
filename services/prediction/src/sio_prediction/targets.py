"""The forecasts an operator actually asks for (PRD M10).

Everything here is built from `series` + `forecasters`, so each target is a choice of *what to measure*
and *how to read the result* rather than a new modelling effort. That is the point of the split: adding
"how busy will the fuel store be" should be a query and a bucket size, not a new model.

Each target declares its own gap policy and bucket size, because those are properties of the quantity
and getting them wrong is worse than choosing the wrong model. An empty bucket in a throughput series
is a real zero; an empty bucket in a temperature series is unknown. Filling the second like the first
predicts a freezing warehouse, confidently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sio_core import get_logger
from sio_core.explain import ExplanationBuilder
from sio_schemas import Forecast, ForecastPoint

from .forecasters import Backtest, ForecastResult, backtest, forecast_series
from .series import GapPolicy, Series

#: Fraction of a known physical range at which an interval stops carrying information.
#:
#: 0.8 rather than 1.0 because the degeneracy is gradual: a 90 % interval covering 80 % of everything a
#: battery can hold is already useless for deciding whether to recall a drone.
UNINFORMATIVE_FRACTION = 0.8

log = get_logger("sio.prediction.targets")


@dataclass(frozen=True)
class TargetSpec:
    """How to build and read one kind of forecast."""

    target: str
    bucket_s: float
    horizon_buckets: int
    policy: GapPolicy
    aggregate: str
    lookback_s: float
    season_buckets: int | None = None
    unit: str | None = None
    non_negative: bool = False
    """Whether values below zero are physically impossible.

    Counts and occupancies cannot be negative, and an interval whose lower bound is -3 vehicles is the
    clearest possible sign that a model has been extrapolated past its usefulness. Clamping is honest
    here in a way it would not be for a temperature, where a negative value is simply cold.
    """
    max_value: float | None = None
    """Physical upper bound, where one exists.

    A battery is a percentage. Live, a 20-minute forecast of a steady 86 % produced an interval of 24 to
    148 per cent — and 148 per cent is not a cautious estimate, it is a nonsense that discredits every
    other number beside it. Clamping to the real range keeps the statement true: "somewhere between 24
    and 100" is wide, and wide is allowed; impossible is not.
    """
    description: str = ""


# The catalogue. Adding a target is a row here plus a query, which is the intended cost.
SPECS: dict[str, TargetSpec] = {
    "throughput": TargetSpec(
        target="throughput",
        bucket_s=60.0,
        horizon_buckets=15,
        policy=GapPolicy.ZERO,
        aggregate="sum",
        lookback_s=3600.0,
        unit="entries_per_min",
        non_negative=True,
        description="Gate entries per minute across the site",
    ),
    "occupancy": TargetSpec(
        target="occupancy",
        bucket_s=60.0,
        horizon_buckets=15,
        policy=GapPolicy.HOLD,
        aggregate="max",
        lookback_s=3600.0,
        unit="entities",
        non_negative=True,
        description="Distinct entities present in a zone",
    ),
    "temperature": TargetSpec(
        target="temperature",
        bucket_s=60.0,
        horizon_buckets=20,
        policy=GapPolicy.HOLD,
        aggregate="mean",
        lookback_s=7200.0,
        unit="celsius",
        description="Zone temperature, for early warning of a thermal event",
    ),
    "battery": TargetSpec(
        target="battery",
        bucket_s=30.0,
        horizon_buckets=40,
        policy=GapPolicy.HOLD,
        aggregate="min",
        lookback_s=1800.0,
        unit="percent",
        non_negative=True,
        max_value=100.0,
        description="Drone battery level, and when it will need to return",
    ),
    "vibration": TargetSpec(
        target="machine_failure_risk",
        bucket_s=60.0,
        horizon_buckets=15,
        policy=GapPolicy.HOLD,
        aggregate="max",
        lookback_s=7200.0,
        unit="mm_s",
        non_negative=True,
        description="Machine vibration trend, as a proxy for developing failure",
    ),
}


@dataclass
class TargetForecast:
    """A forecast plus the evidence for how much to trust it."""

    spec: TargetSpec
    series: Series
    result: ForecastResult
    backtest_result: Backtest | None
    zone_id: str | None = None
    entity_id: str | None = None

    @property
    def points(self) -> list[ForecastPoint]:
        return self.result.points

    def confidence(self) -> float:
        """Confidence grounded in measured interval coverage, not in a feeling.

        Starts from how well this method's intervals actually held up on held-out data for this series,
        and is reduced when the series was largely filled in by the gap policy. A forecast built mostly
        from invented buckets should not be presented with the same confidence as one built from
        observations, and nothing else in the output would reveal the difference.
        """
        base = 0.4
        if self.backtest_result is not None:
            # A calibrated interval is the strongest evidence available that the forecast is usable.
            base = 0.8 if self.backtest_result.calibrated else 0.35
        base *= 0.5 + 0.5 * self.series.coverage
        if self.series.is_flat:
            base *= 0.7  # a flat series may be a stuck sensor rather than a stable quantity
        return round(max(0.05, min(0.95, base)), 3)

    def to_forecast(self, tenant_id: str, *, made_at: datetime) -> Forecast:
        spec = self.spec
        points = self.points
        if spec.non_negative or spec.max_value is not None:
            points = [
                ForecastPoint(
                    ts=point.ts,
                    value=_clamp(point.value, spec),
                    lo=_clamp(point.lo, spec),
                    hi=_clamp(point.hi, spec),
                )
                for point in points
            ]

        explanation = ExplanationBuilder(summary=self._summary(points))
        explanation.add_model(self.result.model_name, note=spec.description or None)
        for note in self.result.notes:
            explanation.add_note(note)
        explanation.add_note(
            f"built from {len(self.series)} buckets of {spec.bucket_s:.0f}s "
            f"({self.series.coverage:.0%} observed, {self.series.gaps} filled by "
            f"the '{spec.policy}' gap policy)"
        )
        if self.backtest_result is not None:
            coverage = self.backtest_result.coverage
            # 100% coverage of a nominal 90% interval is not a good score. It means the interval is far
            # wider than it needs to be, and reading it as accuracy is exactly backwards — the panel was
            # presenting "contained the truth 100% of the time" as a virtue directly above a band spanning
            # a battery's entire range.
            over_wide = coverage >= 0.99 and self.backtest_result.level <= 0.95
            explanation.add_note(
                f"on held-out data the {self.backtest_result.level:.0%} interval contained the truth "
                f"{coverage:.0%} of the time over {self.backtest_result.folds} folds "
                f"(MAE {self.backtest_result.mae:.3g})"
                + (
                    " — which is MORE than asked for, meaning the interval is wider than it needs to be, "
                    "not that the forecast is accurate"
                    if over_wide
                    else ""
                    if self.backtest_result.calibrated
                    else " — NOT calibrated, treat with caution"
                )
            )
        else:
            explanation.add_note(
                "not enough history to backtest the intervals, so their width is unverified"
            )
        if spec.non_negative or spec.max_value is not None:
            low = "0" if spec.non_negative else "-inf"
            high = f"{spec.max_value:g}" if spec.max_value is not None else "inf"
            explanation.add_note(
                f"clamped to the physical range [{low}, {high}]: a bound outside it is impossible, "
                "and one impossible number discredits every other number beside it"
            )

        return Forecast(
            tenant_id=tenant_id,
            target=spec.target,
            zone_id=self.zone_id,
            entity_id=self.entity_id,
            ts=made_at,
            horizon_s=spec.bucket_s * spec.horizon_buckets,
            points=points,
            model_name=self.result.model_name,
            confidence=self.confidence(),
            interval_level=self.result.interval_level,
            explanation=explanation.build(),
        )

    def _interval_is_uninformative(self, point: ForecastPoint) -> bool:
        """Whether the interval has widened until it says nothing.

        Judged against the physical range when one is known — a battery is 0-100, so a band of 0-100 has
        exactly zero information content — and otherwise against the spread of the history, because an
        interval several times wider than everything ever observed is not a prediction either.
        """
        if point.lo is None or point.hi is None:
            return False
        width = point.hi - point.lo
        if self.spec.max_value is not None:
            floor = 0.0 if self.spec.non_negative else min(self.series.values, default=0.0)
            domain = self.spec.max_value - floor
            if domain > 0 and width >= domain * UNINFORMATIVE_FRACTION:
                return True
        observed = self.series.values
        if len(observed) >= 4:
            spread = max(observed) - min(observed)
            level = abs(observed[-1])
            # The spread rule applies only when the spread MEANS something.
            #
            # This is the second time the same mistake has appeared in this codebase, so it is worth naming:
            # a threshold computed as a multiple of a near-zero quantity is not a threshold. The first
            # instance made a two-events-where-there-are-normally-none anomaly into a two-million-sigma
            # event; this one flagged a perfectly reasonable ±4 interval as uninformative because a steady
            # battery had varied by 0.2 over the whole window, and 4 is more than ten times 0.2.
            #
            # So a flat series is judged on its physical range (above) and not on its own lack of variation.
            meaningful = spread > max(level * 0.02, 1e-6)
            # Ten times the observed range. Generous on purpose: a genuinely uncertain forecast should be
            # allowed to be wide, and only the degenerate case is called out.
            if meaningful and width > spread * 10:
                return True
        return False

    def _summary(self, points: list[ForecastPoint] | None = None) -> str:
        """Describe the forecast, from the SAME points the forecast carries.

        It previously read the unclamped points, so an occupancy summary said "-0.167 to 2.17" while the
        data beside it said "0 to 2.17". A summary that contradicts its own numbers costs more trust than
        it saves effort — and it is the part a human actually reads.
        """
        points = points if points is not None else self.points
        if not points:
            return f"No {self.spec.target} forecast: insufficient history"
        last = self.series.values[-1]
        end = points[-1]
        minutes = self.spec.bucket_s * self.spec.horizon_buckets / 60
        direction = "steady"
        if end.value > last * 1.15 + 0.01:
            direction = "rising"
        elif end.value < last * 0.85 - 0.01:
            direction = "falling"
        where = f" in {self.zone_id}" if self.zone_id else ""
        if self._interval_is_uninformative(end):
            # A band covering the whole plausible range is not a forecast, it is a restatement of the
            # variable's definition. Observed in the running system: "battery steady: 88.1 now, 88.1
            # predicted in 20 min (0 to 100 at 90%)" — the central estimate was good to about 2 and the
            # interval printed beside it spanned every value a battery can hold.
            #
            # Saying so is the only honest option. Quietly narrowing the band would be inventing
            # confidence, and printing it without comment invites a decision the data cannot support.
            return (
                f"{self.spec.target}{where} {direction}: {last:.3g} now, {end.value:.3g} predicted in "
                f"{minutes:.0f} min — but the {self.result.interval_level:.0%} interval spans "
                f"{end.lo:.3g} to {end.hi:.3g}, effectively the whole range, so treat the direction "
                f"rather than the number"
            )
        return (
            f"{self.spec.target}{where} {direction}: {last:.3g} now, "
            f"{end.value:.3g} predicted in {minutes:.0f} min "
            f"({end.lo:.3g} to {end.hi:.3g} at {self.result.interval_level:.0%})"
            if end.lo is not None and end.hi is not None
            else f"{self.spec.target}{where} {direction}: {end.value:.3g} predicted in {minutes:.0f} min"
        )


def build(
    spec: TargetSpec,
    series: Series,
    *,
    level: float = 0.9,
    zone_id: str | None = None,
    entity_id: str | None = None,
    run_backtest: bool = True,
) -> TargetForecast:
    """Forecast one series against one spec, and measure the intervals while we are here."""
    season = spec.season_buckets
    result = forecast_series(
        series, horizon=spec.horizon_buckets, level=level, season_length=season
    )
    measured = (
        backtest(series, horizon=min(5, spec.horizon_buckets), level=level, season_length=season)
        if run_backtest
        else None
    )
    return TargetForecast(
        spec=spec,
        series=series,
        result=result,
        backtest_result=measured,
        zone_id=zone_id,
        entity_id=entity_id,
    )


def _clamp(value: float | None, spec: TargetSpec) -> float | None:
    """Hold a value inside the quantity's physical range."""
    if value is None:
        return None
    if spec.non_negative:
        value = max(0.0, value)
    if spec.max_value is not None:
        value = min(spec.max_value, value)
    return value


def time_to_threshold(
    points: list[ForecastPoint], *, threshold: float, falling: bool = True
) -> float | None:
    """Seconds until the forecast crosses a threshold, using the *interval*, not the centre.

    Deliberately pessimistic for a falling quantity: a drone should turn back when its battery *might*
    hit the reserve, not when its central estimate does. Taking the point forecast here is how a fleet
    ends up with an aircraft down in a yard, having been technically correct on average.
    """
    for point in points:
        bound = (point.lo if falling else point.hi) or point.value
        crossed = bound <= threshold if falling else bound >= threshold
        if crossed:
            return max(0.0, (point.ts - points[0].ts).total_seconds())
    return None


def congestion_from_occupancy(
    forecast: TargetForecast, *, capacity: int | None
) -> dict[str, Any] | None:
    """Turn an occupancy forecast into a congestion statement.

    Congestion is not a separate measurement; it is occupancy read against capacity. Modelling it
    independently would produce two numbers that could disagree about the same zone, and an operator
    with two contradictory answers has none.
    """
    if not capacity or not forecast.points:
        return None
    breach = next(
        (
            point
            for point in forecast.points
            if (point.hi if point.hi is not None else point.value) >= capacity
        ),
        None,
    )
    peak = max((point.hi if point.hi is not None else point.value) for point in forecast.points)
    return {
        "zone_id": forecast.zone_id,
        "capacity": capacity,
        "predicted_peak": round(peak, 2),
        "will_exceed": breach is not None,
        # The upper bound, not the centre: the useful question is whether it *might* overflow.
        "eta_s": round((breach.ts - forecast.points[0].ts).total_seconds(), 1) if breach else None,
        "headroom": round(capacity - peak, 2),
    }
