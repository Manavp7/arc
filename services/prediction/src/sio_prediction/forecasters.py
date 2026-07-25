"""Forecasters behind one port, and a way to check whether their intervals are honest (PRD M10).

A point forecast is an opinion. An *interval* is a claim that can be checked, and checking it is the
only thing that makes the confidence attached to a forecast mean anything. So this module contains
three things, in ascending order of importance:

1. `StatsForecastForecaster` — AutoETS, the PRD's choice, which selects among error/trend/season forms
   by information criterion and derives intervals from the fitted state-space model.
2. `DriftForecaster` — a fallback with *empirically* calibrated intervals, used when the series is too
   short for AutoETS to mean anything or when the dependency is absent.
3. `backtest` — hold out the tail, forecast it, and count how often the truth landed inside the
   interval. A 90 per cent interval that contains the truth 55 per cent of the time is not a
   conservative forecast, it is a broken one, and nothing in the forecast itself would say so.

The port exists because AutoETS is not always the right answer and the honest way to find out is to
measure. `select_forecaster` chooses on the shape of the data, not on preference.
"""

from __future__ import annotations

import itertools
import math
import statistics
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from sio_core import get_logger
from sio_schemas import ForecastPoint

from .series import Series

log = get_logger("sio.prediction.forecasters")

# AutoETS needs enough points to distinguish trend and season from noise. Below this a fitted
# state-space model is fitting the noise, and its intervals inherit that false confidence.
MIN_POINTS_FOR_ETS = 20
# Two full cycles before a seasonal term is even considered. One cycle cannot tell seasonality from
# trend, and a model that "finds" a season in one cycle will extrapolate it forever.
MIN_CYCLES_FOR_SEASON = 2


@dataclass
class ForecastResult:
    """Points, plus everything needed to judge how much to believe them."""

    points: list[ForecastPoint]
    model_name: str
    interval_level: float
    residual_sigma: float | None = None
    notes: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.notes is None:
            self.notes = []

    @property
    def is_empty(self) -> bool:
        return not self.points


class Forecaster(Protocol):
    """The seam. Anything that can turn a series into points with intervals."""

    name: str

    def forecast(self, series: Series, *, horizon: int, level: float = 0.9) -> ForecastResult: ...


class DriftForecaster:
    """Damped linear drift with intervals from the residuals of its own one-step-ahead errors.

    Not a placeholder. For a short, noisy series this is frequently the better choice, and its
    intervals have a property AutoETS's do not: they are measured rather than derived, so they cannot
    be narrow for the wrong reason. The width comes from how badly this same method did on this same
    series one step at a time.

    Two details that matter more than the trend estimate:

    * **The drift is damped** (`phi < 1`). An undamped linear extrapolation of a noisy slope produces
      absurd values within a few steps — a yard forecast that predicts negative occupancy — and the
      further out you go the more confident it looks.
    * **Interval width grows with the square root of the horizon**, which is what accumulating
      independent errors does. Constant-width intervals are the most common way a forecast lies about
      the far end of its own horizon.
    """

    name = "drift"

    def __init__(self, *, damping: float = 0.85, min_points: int = 4) -> None:
        self.damping = damping
        self.min_points = min_points

    def forecast(self, series: Series, *, horizon: int, level: float = 0.9) -> ForecastResult:
        values = list(series.values)
        if len(values) < self.min_points:
            return ForecastResult(
                points=[],
                model_name=self.name,
                interval_level=level,
                notes=[f"only {len(values)} points; {self.min_points} needed"],
            )

        # A robust slope: the median of successive differences over the recent tail. The mean of
        # differences is exactly the endpoint difference divided by n, so one wild final sample sets
        # the entire trend — which is how a forecast ends up driven by the worst reading in the window.
        tail = values[-min(len(values), 12) :]
        diffs = [second - first for first, second in itertools.pairwise(tail)]
        slope = statistics.median(diffs) if diffs else 0.0
        level_estimate = statistics.fmean(values[-3:])

        residuals = self._one_step_residuals(values, slope)
        sigma = _robust_sigma(residuals, level=level_estimate) if residuals else 0.0
        z = _z_for(level)

        points: list[ForecastPoint] = []
        cumulative = 0.0
        for step in range(1, horizon + 1):
            cumulative += self.damping**step
            centre = level_estimate + slope * cumulative
            # sqrt(step) growth: independent one-step errors accumulate in variance, not in width.
            spread = z * sigma * math.sqrt(step)
            ts = series.end + timedelta(seconds=series.bucket_s * step)
            points.append(
                ForecastPoint(
                    ts=ts,
                    value=round(centre, 4),
                    lo=round(centre - spread, 4),
                    hi=round(centre + spread, 4),
                )
            )
        return ForecastResult(
            points=points,
            model_name=self.name,
            interval_level=level,
            residual_sigma=round(sigma, 4),
            notes=[
                f"damped drift (phi={self.damping}) with slope {slope:+.4g} per bucket",
                f"interval from measured one-step residuals (sigma {sigma:.4g}), widening as sqrt(h)",
            ],
        )

    def _one_step_residuals(self, values: list[float], slope: float) -> list[float]:
        """How wrong this method is one step ahead, on this series. The interval's evidence."""
        return [actual - (previous + slope) for previous, actual in itertools.pairwise(values)]


class StatsForecastForecaster:
    """AutoETS via StatsForecast — the PRD's choice for M10.

    Imported lazily. The import costs about three seconds of numba and pandas warm-up, and paying that
    at service start (or in every unit test run) for a model that may not be used is a poor trade.

    Seasonality is only offered when the series covers at least two full cycles. With one cycle a
    seasonal model cannot distinguish season from trend, and having "found" a season it will project it
    forward indefinitely with confident intervals.
    """

    name = "autoets"

    def __init__(self, *, season_length: int | None = None) -> None:
        self.season_length = season_length
        self._available: bool | None = None

    def available(self) -> bool:
        if self._available is None:
            try:
                import statsforecast  # noqa: F401

                self._available = True
            except ImportError:
                log.warning(
                    "prediction.statsforecast_missing",
                    effect="falling back to damped drift with empirical intervals",
                )
                self._available = False
        return self._available

    def forecast(self, series: Series, *, horizon: int, level: float = 0.9) -> ForecastResult:
        if not self.available():
            return ForecastResult(
                points=[],
                model_name=self.name,
                interval_level=level,
                notes=["statsforecast not installed"],
            )
        if len(series) < MIN_POINTS_FOR_ETS:
            return ForecastResult(
                points=[],
                model_name=self.name,
                interval_level=level,
                notes=[
                    f"{len(series)} points is too few for AutoETS ({MIN_POINTS_FOR_ETS} needed)"
                ],
            )
        if series.is_flat:
            # A state-space fit on a constant series yields a zero-width interval, which claims
            # certainty about a sensor that may simply be stuck. The drift forecaster's empirical
            # interval degrades more gracefully here.
            return ForecastResult(
                points=[],
                model_name=self.name,
                interval_level=level,
                notes=["series is flat; a fitted interval would claim false certainty"],
            )

        import pandas as pd
        from statsforecast import StatsForecast
        from statsforecast.models import AutoETS

        season = self.season_length or 0
        if season and len(series) < season * MIN_CYCLES_FOR_SEASON:
            season = 0  # not enough history to claim a season
        frame = pd.DataFrame(
            {
                "unique_id": series.name,
                "ds": pd.to_datetime([ts.replace(tzinfo=None) for ts in series.timestamps]),
                "y": list(series.values),
            }
        )
        model = AutoETS(season_length=season) if season else AutoETS()
        # A pandas offset ALIAS, not a Timedelta. Passing a Timedelta sends pandas down a code path
        # that does bare-integer timedelta arithmetic, which numpy now deprecates — and the project
        # treats warnings as errors, so it surfaced as eleven failing tests rather than as a note.
        freq = f"{round(series.bucket_s)}s"
        engine = StatsForecast(models=[model], freq=freq, n_jobs=1)
        percentage = round(level * 100)
        try:
            output = engine.forecast(df=frame, h=horizon, level=[percentage])
        except Exception as exc:
            log.warning("prediction.autoets_failed", series=series.name, error=str(exc))
            return ForecastResult(
                points=[],
                model_name=self.name,
                interval_level=level,
                notes=[f"AutoETS failed: {exc}"],
            )

        centre_column = "AutoETS"
        lo_column, hi_column = f"AutoETS-lo-{percentage}", f"AutoETS-hi-{percentage}"
        points: list[ForecastPoint] = []
        for step, (_, row) in enumerate(output.iterrows(), start=1):
            ts = series.end + timedelta(seconds=series.bucket_s * step)
            lo = float(row[lo_column]) if lo_column in row else None
            hi = float(row[hi_column]) if hi_column in row else None
            if lo is not None and hi is not None and lo > hi:
                lo, hi = hi, lo  # the schema rejects an inverted interval, and rightly
            points.append(
                ForecastPoint(
                    ts=ts,
                    value=round(float(row[centre_column]), 4),
                    lo=round(lo, 4) if lo is not None else None,
                    hi=round(hi, 4) if hi is not None else None,
                )
            )
        return ForecastResult(
            points=points,
            model_name=self.name,
            interval_level=level,
            notes=[
                f"AutoETS selected by information criterion over {len(series)} points"
                + (f", season length {season}" if season else ", no seasonal term"),
            ],
        )


def select_forecaster(series: Series, *, season_length: int | None = None) -> Forecaster:
    """Choose on the shape of the data, not on preference.

    AutoETS where there is enough history for it to mean something; damped drift with empirical
    intervals otherwise. The fallback is not a lesser path — for a short or flat series it is the more
    honest one, because its interval is measured rather than derived from a model fitted to noise.
    """
    ets = StatsForecastForecaster(season_length=season_length)
    if len(series) >= MIN_POINTS_FOR_ETS and not series.is_flat and ets.available():
        return ets
    return DriftForecaster()


def forecast_series(
    series: Series, *, horizon: int, level: float = 0.9, season_length: int | None = None
) -> ForecastResult:
    """Forecast with the best available method, falling back rather than failing.

    A forecaster returning no points is a decision, not an error — "too little history" is the right
    answer sometimes — so the fallback is tried and the reason is carried through in the notes.
    """
    primary = select_forecaster(series, season_length=season_length)
    result = primary.forecast(series, horizon=horizon, level=level)
    if not result.is_empty:
        return result
    if primary.name != "drift":
        fallback = DriftForecaster().forecast(series, horizon=horizon, level=level)
        fallback.notes = [*result.notes, *fallback.notes]
        return fallback
    return result


# --------------------------------------------------------------------------- backtest
@dataclass
class Backtest:
    """How the intervals actually performed on held-out data."""

    series: str
    model_name: str
    level: float
    folds: int
    coverage: float
    """Fraction of held-out points that fell inside the interval. Should approximate ``level``."""
    mae: float
    mean_interval_width: float
    notes: list[str]

    @property
    def calibrated(self) -> bool:
        """Within a reasonable tolerance of the nominal level.

        Deliberately two-sided. Intervals that are too *wide* are also a failure: an interval
        containing every possible value is trivially correct and tells an operator nothing.
        """
        return abs(self.coverage - self.level) <= 0.2

    def describe(self) -> dict[str, Any]:
        return {
            "series": self.series,
            "model": self.model_name,
            "level": self.level,
            "folds": self.folds,
            "coverage": round(self.coverage, 3),
            "calibrated": self.calibrated,
            "mae": round(self.mae, 4),
            "mean_interval_width": round(self.mean_interval_width, 4),
            "notes": self.notes,
        }


def backtest(
    series: Series,
    *,
    horizon: int = 5,
    level: float = 0.9,
    folds: int = 4,
    season_length: int | None = None,
) -> Backtest | None:
    """Rolling-origin evaluation: how often did the truth land inside the interval?

    This is the only thing that makes a stated confidence meaningful. A 90 per cent interval that
    contains the truth 55 per cent of the time is not conservative, it is wrong, and no amount of
    reading the forecast would reveal it.

    Rolling origin rather than a single split: one hold-out measures one accident of where the split
    fell. Each fold trains only on data preceding its own hold-out, so nothing leaks backwards.
    """
    minimum = horizon * 2 + 6
    if len(series) < minimum + folds:
        return None

    inside = 0
    total = 0
    absolute_errors: list[float] = []
    widths: list[float] = []
    model_names: set[str] = set()
    notes: list[str] = []

    for fold in range(folds):
        cut = len(series) - horizon - fold
        if cut < minimum - horizon:
            break
        train = Series(
            name=series.name,
            bucket_s=series.bucket_s,
            start=series.start,
            values=series.values[:cut],
            gaps=0,
            unit=series.unit,
        )
        actual = series.values[cut : cut + horizon]
        result = forecast_series(train, horizon=horizon, level=level, season_length=season_length)
        if result.is_empty:
            notes.append(f"fold {fold}: no forecast ({'; '.join(result.notes)})")
            continue
        model_names.add(result.model_name)
        for point, truth in zip(result.points, actual, strict=False):
            total += 1
            absolute_errors.append(abs(point.value - truth))
            if point.lo is not None and point.hi is not None:
                widths.append(point.hi - point.lo)
                if point.lo <= truth <= point.hi:
                    inside += 1

    if total == 0:
        return None
    return Backtest(
        series=series.name,
        model_name="+".join(sorted(model_names)) or "none",
        level=level,
        folds=folds,
        coverage=inside / total,
        mae=statistics.fmean(absolute_errors),
        mean_interval_width=statistics.fmean(widths) if widths else 0.0,
        notes=notes,
    )


def _robust_sigma(residuals: list[float], *, level: float = 0.0) -> float:
    """Spread from the median absolute deviation, floored relative to the quantity's magnitude.

    Robust rather than a plain standard deviation, for the same reason as in anomaly detection: one
    outlier inflates a standard deviation, and here that would widen every interval until the forecast
    said nothing at all.

    The floor matters more than it looks. A perfectly flat series has zero residuals, so the raw sigma
    is zero and every interval collapses to the point forecast — the model claiming perfect knowledge of
    a sensor that may simply be stuck. Flooring at half a per cent of the level says "a quantity sitting
    at 20.0 could still move by a tenth", which is true and is the least the interval can honestly
    admit. An absolute floor would be wrong at both ends of the scale: meaningless for a percentage,
    absurd for a pressure in pascals.
    """
    if not residuals:
        return 0.0
    median = statistics.median(residuals)
    mad = statistics.median([abs(value - median) for value in residuals])
    sigma = mad * 1.4826
    if sigma <= 0:
        # All residuals identical, usually all zero. Try the standard deviation before the floor.
        sigma = statistics.pstdev(residuals) if len(residuals) > 1 else 0.0
    return max(sigma, abs(level) * 0.005, 1e-4)


def _z_for(level: float) -> float:
    """Normal quantile for a two-sided interval.

    A small table rather than scipy: three lookups do not justify the dependency, and interpolating
    between them is more honest than pretending to a precision the residual estimate does not have.
    """
    table = {0.5: 0.674, 0.8: 1.282, 0.9: 1.645, 0.95: 1.960, 0.99: 2.576}
    if level in table:
        return table[level]
    keys = sorted(table)
    if level <= keys[0]:
        return table[keys[0]]
    if level >= keys[-1]:
        return table[keys[-1]]
    for low, high in itertools.pairwise(keys):
        if low <= level <= high:
            fraction = (level - low) / (high - low)
            return table[low] + fraction * (table[high] - table[low])
    return 1.645
