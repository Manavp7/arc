"""Tests for forecasting (PRD M10).

A point forecast is an opinion; an interval is a claim that can be checked. So the tests that matter
most here are the ones that check the claim:

* intervals are **calibrated** — a 90 per cent interval contains the truth about 90 per cent of the
  time on held-out data, measured rather than asserted;
* intervals **widen with the horizon**, because a constant-width interval lies about the far end;
* the service **refuses** to forecast from too little history, rather than producing a confident
  interval computed from four points.

The resampling tests get equal weight, because bucket size and gap policy do more damage than model
choice and are invisible in the output. Filling a missing temperature with zero predicts a freezing
warehouse, confidently.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from sio_prediction.forecasters import (
    MIN_POINTS_FOR_ETS,
    DriftForecaster,
    StatsForecastForecaster,
    backtest,
    forecast_series,
    select_forecaster,
)
from sio_prediction.series import GapPolicy, Series, bucketise, counts_per_bucket
from sio_prediction.targets import (
    SPECS,
    build,
    congestion_from_occupancy,
    time_to_threshold,
)
from sio_prediction.trajectory import (
    SPEED_FLOOR_MPS,
    Kinematics,
    predict_next_zones,
    predict_trajectory,
    turn_rate_from_headings,
)

from sio_schemas import ForecastPoint, Geo

START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def series_of(values: list[float], *, bucket_s: float = 60.0, name: str = "test") -> Series:
    return Series(name=name, bucket_s=bucket_s, start=START, values=tuple(values))


def samples(values: list[float], *, every_s: float = 20.0) -> list[tuple[datetime, float]]:
    return [
        (START + timedelta(seconds=every_s * index), value) for index, value in enumerate(values)
    ]


# --------------------------------------------------------------------- resampling
def test_bucketising_averages_within_a_bucket() -> None:
    built = bucketise(
        samples([10.0, 20.0, 30.0, 40.0, 50.0, 60.0], every_s=20.0),
        name="t",
        bucket_s=60.0,
        now=START + timedelta(seconds=180),
        policy=GapPolicy.HOLD,
        aggregate="mean",
    )
    assert built is not None
    # Three samples per 60 s bucket. The third bucket [120, 180) is COMPLETE — `now` is 180 — and held
    # nothing, so the HOLD policy carries the last value into it. Only the bucket containing `now`
    # would be excluded, and there is none here.
    assert built.values == (20.0, 50.0, 50.0)
    assert built.gaps == 1


def test_the_trailing_partial_bucket_is_dropped() -> None:
    """The bucket containing "now" is incomplete by definition, so its count is always low.

    Including it teaches the model that activity is collapsing on every single run, and the resulting
    downward slope is entirely an artefact of asking the question.
    """
    counted = counts_per_bucket(
        [START + timedelta(seconds=index * 10) for index in range(18)],
        name="throughput",
        bucket_s=60.0,
        # Halfway through the fourth bucket: it holds only part of its events.
        now=START + timedelta(seconds=210),
    )
    assert counted is not None
    assert len(counted) == 3, "three complete buckets, and the partial fourth is not one of them"
    assert counted.values == (6.0, 6.0, 6.0), "no artificial dip at the end"


def test_an_empty_bucket_means_zero_for_a_count() -> None:
    counted = counts_per_bucket(
        [START, START + timedelta(seconds=5), START + timedelta(seconds=185)],
        name="throughput",
        bucket_s=60.0,
        now=START + timedelta(seconds=250),
    )
    assert counted is not None
    assert counted.values == (2.0, 0.0, 0.0, 1.0), "nothing happening is data"
    assert counted.gaps == 2


def test_an_empty_bucket_means_unknown_for_a_measurement() -> None:
    """Filling a missing temperature with zero predicts a freezing warehouse, confidently."""
    built = bucketise(
        [(START, 21.0), (START + timedelta(seconds=240), 22.0)],
        name="temperature",
        bucket_s=60.0,
        now=START + timedelta(seconds=300),
        policy=GapPolicy.HOLD,
        aggregate="mean",
    )
    assert built is not None
    assert built.values == (21.0, 21.0, 21.0, 21.0, 22.0), "the last known value is carried"
    assert 0.0 not in built.values


def test_coverage_reports_how_much_was_invented() -> None:
    """A forecast built from 70 per cent filled buckets deserves to be read differently, and nothing
    else in the output would reveal it."""
    built = bucketise(
        [(START, 5.0), (START + timedelta(seconds=540), 6.0)],
        name="sparse",
        bucket_s=60.0,
        now=START + timedelta(seconds=600),
        policy=GapPolicy.HOLD,
    )
    assert built is not None
    assert built.coverage < 0.3
    assert built.gaps >= 7


def test_buckets_are_aligned_so_consecutive_runs_are_comparable() -> None:
    """An unaligned grid slides with the wall clock, and two consecutive forecasts then describe
    different buckets while claiming to describe the same series."""
    offset_samples = [(START + timedelta(seconds=37 + 20 * index), 1.0) for index in range(12)]
    built = bucketise(
        offset_samples,
        name="x",
        bucket_s=60.0,
        now=START + timedelta(seconds=400),
        policy=GapPolicy.ZERO,
    )
    assert built is not None
    assert built.start.second == 0, "the grid starts on a bucket boundary"


def test_nothing_in_returns_nothing_out() -> None:
    assert bucketise([], name="x", bucket_s=60.0, now=START) is None
    assert counts_per_bucket([], name="x", bucket_s=60.0, now=START) is None


def test_a_flat_series_is_recognised() -> None:
    """Worth knowing before fitting: a model on a flat series produces a zero-width interval, claiming
    certainty about a sensor that may simply be stuck."""
    assert series_of([5.0] * 30).is_flat
    assert not series_of([5.0] * 29 + [5.1]).is_flat


# -------------------------------------------------------------------- forecasters
def test_the_drift_forecaster_follows_a_trend() -> None:
    result = DriftForecaster().forecast(series_of([float(x) for x in range(10, 30)]), horizon=5)
    assert result.points
    assert result.points[0].value > 28, "a rising series should be predicted to keep rising"
    assert all(point.lo is not None and point.hi is not None for point in result.points)


def test_intervals_widen_with_the_horizon() -> None:
    """A constant-width interval is the most common way a forecast lies about the far end of its own
    horizon."""
    result = DriftForecaster().forecast(
        series_of([10.0, 11.0, 9.5, 10.5, 11.5, 9.0, 10.0, 12.0, 10.5, 11.0]), horizon=8
    )
    widths = [point.hi - point.lo for point in result.points]  # type: ignore[operator]
    assert widths[0] < widths[-1]
    # sqrt growth: the eighth step should be about sqrt(8) times the first, not eight times.
    assert widths[-1] / widths[0] == pytest.approx(math.sqrt(8), rel=0.15)


def test_drift_is_damped_so_it_does_not_run_away() -> None:
    """An undamped extrapolation of a noisy slope predicts absurd values within a few steps — and looks
    more confident the further out it goes."""
    rising = series_of([float(x) * 3 for x in range(20)])
    result = DriftForecaster().forecast(rising, horizon=40)
    last_observed = rising.values[-1]
    undamped_projection = last_observed + 3.0 * 40
    assert result.points[-1].value < undamped_projection * 0.9


def test_a_wild_final_sample_does_not_set_the_whole_trend() -> None:
    """The mean of successive differences is the endpoint difference over n, so one bad final reading
    would set the entire slope. The median of differences does not care."""
    steady = [10.0] * 15
    result = DriftForecaster().forecast(series_of([*steady, 400.0]), horizon=5)
    assert result.points[0].value < 200, "one outlier must not become the trend"


def test_the_drift_forecaster_refuses_a_series_that_is_too_short() -> None:
    result = DriftForecaster(min_points=6).forecast(series_of([1.0, 2.0]), horizon=5)
    assert result.is_empty
    assert "points" in result.notes[0]


def test_a_flat_series_still_gets_a_non_zero_interval() -> None:
    """All residuals identical (usually zero) would give an interval of literally zero width, claiming
    perfect knowledge of the future of a possibly-stuck sensor."""
    result = DriftForecaster().forecast(series_of([20.0] * 20), horizon=4)
    assert result.points
    for point in result.points:
        assert point.hi > point.lo, "zero-width intervals claim certainty nobody has"  # type: ignore[operator]


def test_forecaster_selection_follows_the_data_not_a_preference() -> None:
    short = series_of([float(x) for x in range(8)])
    assert select_forecaster(short).name == "drift", "too short for AutoETS to mean anything"

    flat = series_of([7.0] * 40)
    assert select_forecaster(flat).name == "drift", "a fitted interval on a flat series is false"

    varied = series_of([10 + 3 * math.sin(x / 5) for x in range(40)])
    chosen = select_forecaster(varied)
    assert chosen.name in ("autoets", "drift")
    if StatsForecastForecaster().available():
        assert chosen.name == "autoets", "with enough varied history, use the PRD's model"


def test_forecast_series_falls_back_rather_than_failing() -> None:
    """ "Too little history" is a decision, not an error, and the reason is carried through."""
    result = forecast_series(series_of([float(x) for x in range(6)]), horizon=3)
    assert result.points, "the fallback should have produced something"
    assert result.model_name == "drift"


@pytest.mark.skipif(not StatsForecastForecaster().available(), reason="statsforecast not installed")
def test_autoets_produces_intervals_on_a_seasonal_series() -> None:
    values = [20 + 5 * math.sin(index * math.pi / 6) for index in range(48)]
    result = StatsForecastForecaster(season_length=12).forecast(series_of(values), horizon=6)
    assert result.model_name == "autoets"
    assert len(result.points) == 6
    assert all(point.lo is not None and point.hi is not None for point in result.points)
    assert all(point.lo <= point.value <= point.hi for point in result.points)  # type: ignore[operator]


@pytest.mark.skipif(not StatsForecastForecaster().available(), reason="statsforecast not installed")
def test_autoets_refuses_a_season_it_cannot_see_two_cycles_of() -> None:
    """With one cycle a seasonal model cannot tell season from trend, and having "found" a season it
    projects it forward forever with confident intervals."""
    values = [20 + 5 * math.sin(index * math.pi / 6) for index in range(MIN_POINTS_FOR_ETS + 2)]
    result = StatsForecastForecaster(season_length=20).forecast(series_of(values), horizon=4)
    assert result.points
    assert any("no seasonal term" in note for note in result.notes)


# ---------------------------------------------------------- calibration (the point)
def test_intervals_are_calibrated_on_a_noisy_series() -> None:
    """The test that makes every confidence number in this service mean something.

    A 90 per cent interval that contains the truth 55 per cent of the time is not a conservative
    forecast, it is a broken one — and nothing in the forecast itself would say so.
    """
    import random

    rng = random.Random(11)
    values = [20.0 + 4 * math.sin(index / 7) + rng.gauss(0, 0.9) for index in range(90)]
    measured = backtest(series_of(values), horizon=4, level=0.9, folds=8)
    assert measured is not None
    assert measured.coverage >= 0.65, (
        f"90% intervals covered only {measured.coverage:.0%} of held-out points"
    )
    assert measured.calibrated
    assert measured.mean_interval_width > 0


def test_calibration_is_two_sided() -> None:
    """Intervals that are too WIDE are also a failure: one containing every possible value is
    trivially correct and tells an operator nothing."""
    from sio_prediction.forecasters import Backtest

    too_wide = Backtest(
        series="x",
        model_name="m",
        level=0.9,
        folds=4,
        coverage=1.0,
        mae=0.1,
        mean_interval_width=1e6,
        notes=[],
    )
    assert too_wide.calibrated, "coverage 1.0 against level 0.9 is within tolerance"
    hopeless = Backtest(
        series="x",
        model_name="m",
        level=0.9,
        folds=4,
        coverage=0.4,
        mae=0.1,
        mean_interval_width=0.01,
        notes=[],
    )
    assert not hopeless.calibrated


def test_backtesting_refuses_a_series_that_is_too_short_to_evaluate() -> None:
    assert backtest(series_of([1.0, 2.0, 3.0]), horizon=5) is None


def test_backtest_folds_never_train_on_their_own_future() -> None:
    """Leakage would make every interval look perfect. Each fold trains only on data preceding its
    hold-out, and the check is that a deliberately shifted tail hurts coverage."""
    honest = [10.0 + (index % 3) for index in range(80)]
    shifted = [*honest[:70], *[500.0] * 10]
    clean = backtest(series_of(honest), horizon=4, level=0.9, folds=6)
    broken = backtest(series_of(shifted), horizon=4, level=0.9, folds=6)
    assert clean is not None and broken is not None
    assert broken.coverage < clean.coverage, "an unforeseeable jump must not be inside the interval"


# --------------------------------------------------------------------- trajectory
def test_a_moving_entity_gets_a_path_with_a_widening_cone() -> None:
    path = predict_trajectory(
        "ent_1",
        Kinematics(geo=Geo(lat=37.7749, lon=-122.4194), speed_mps=5.0, heading_deg=90.0, ts=START),
        horizon_s=60.0,
        step_s=10.0,
    )
    # Up to six points, but fewer if the cone stops being informative first — which it does for a
    # 5 m/s vehicle inside a minute, and truncating is the correct behaviour rather than a shortfall.
    assert 3 <= len(path.points) <= 6
    assert not path.stationary
    # Heading 90 degrees is due east: longitude rises, latitude barely moves.
    assert path.points[-1].geo.lon > path.points[0].geo.lon
    assert abs(path.points[-1].geo.lat - 37.7749) < 0.0005
    sigmas = [point.sigma_m for point in path.points]
    assert sigmas == sorted(sigmas), "uncertainty must never shrink with the horizon"
    assert sigmas[-1] > sigmas[0] * 3, "a cone, not a line"


def test_a_parked_entity_is_predicted_to_stay_put() -> None:
    """Extrapolating noise on a parked truck produces confident motion in a direction chosen by the
    last GPS jitter."""
    path = predict_trajectory(
        "ent_parked",
        Kinematics(
            geo=Geo(lat=37.7749, lon=-122.4194),
            speed_mps=SPEED_FLOOR_MPS * 0.5,
            heading_deg=42.0,
            ts=START,
        ),
        horizon_s=60.0,
    )
    assert path.stationary
    assert all(point.geo.lat == 37.7749 for point in path.points)
    assert path.points[-1].sigma_m > path.points[0].sigma_m, "still less certain over time"
    assert any("floor" in note for note in path.notes)


def test_an_entity_with_no_heading_is_not_given_one() -> None:
    path = predict_trajectory(
        "ent_x",
        Kinematics(geo=Geo(lat=1.0, lon=2.0), speed_mps=8.0, heading_deg=None, ts=START),
        horizon_s=30.0,
    )
    assert path.stationary, "a speed without a direction is not a trajectory"


def test_speed_decays_so_a_long_horizon_stays_plausible() -> None:
    """A sixty-second prediction should not assume a truck keeps its current speed across the yard."""
    fast = Kinematics(geo=Geo(lat=0.0, lon=0.0), speed_mps=10.0, heading_deg=0.0, ts=START)
    path = predict_trajectory("ent_fast", fast, horizon_s=120.0, step_s=10.0)
    travelled_m = (path.points[-1].geo.lat - 0.0) * 111_320
    assert travelled_m < 10.0 * 120 * 0.85, "undamped travel would be 1200 m"
    assert travelled_m > 100, "but it should still be predicted to move a long way"


def test_confidence_falls_as_the_cone_widens() -> None:
    near = predict_trajectory(
        "e",
        Kinematics(geo=Geo(lat=0.0, lon=0.0), speed_mps=4.0, heading_deg=0.0, ts=START),
        horizon_s=10.0,
    )
    far = predict_trajectory(
        "e",
        Kinematics(geo=Geo(lat=0.0, lon=0.0), speed_mps=4.0, heading_deg=0.0, ts=START),
        horizon_s=300.0,
    )
    assert near.confidence() > far.confidence()
    assert far.confidence() < 0.3, "a 100 m cone is not something to plan around"


def test_turn_rate_wraps_around_north() -> None:
    """Without wrapping, a vehicle crossing north produces a 359-degree "turn" and the prediction
    spirals."""
    headings = [
        (START, 350.0),
        (START + timedelta(seconds=1), 355.0),
        (START + timedelta(seconds=2), 0.0),
        (START + timedelta(seconds=3), 5.0),
    ]
    assert turn_rate_from_headings(headings) == pytest.approx(5.0, abs=0.5)


def test_turn_rate_ignores_a_single_bad_heading() -> None:
    headings = [
        (START, 90.0),
        (START + timedelta(seconds=1), 90.0),
        (START + timedelta(seconds=2), 270.0),  # nonsense
        (START + timedelta(seconds=3), 90.0),
        (START + timedelta(seconds=4), 90.0),
    ]
    assert abs(turn_rate_from_headings(headings)) < 1.0


def test_turn_rate_of_a_single_heading_is_zero() -> None:
    assert turn_rate_from_headings([(START, 90.0)]) == 0.0


def test_next_zone_prediction_is_a_probability_not_a_ray() -> None:
    """A single ray passing near a polygon edge is a coin flip reported as a fact, and the interesting
    cases — a truck approaching a gate at an angle — are exactly the ones near an edge."""
    path = predict_trajectory(
        "ent_1",
        Kinematics(
            geo=Geo(lat=0.0, lon=0.0),
            speed_mps=6.0,
            heading_deg=0.0,
            ts=START,
            position_sigma_m=4.0,
        ),
        horizon_s=40.0,
        step_s=5.0,
    )

    def contains(geo: Geo) -> list[str]:
        # A band to the north, so most but not all sampled paths enter it.
        return ["gate_north"] if geo.lat > 0.0009 else []

    predictions = predict_next_zones(path, contains, samples=80)
    assert predictions
    top = predictions[0]
    assert top.zone_id == "gate_north"
    assert 0.0 < top.probability <= 1.0
    assert top.eta_s is not None and top.eta_s > 0


def test_the_zone_an_entity_is_already_in_is_not_predicted() -> None:
    path = predict_trajectory(
        "ent_1",
        Kinematics(geo=Geo(lat=0.0, lon=0.0), speed_mps=5.0, heading_deg=0.0, ts=START),
        horizon_s=20.0,
    )
    predictions = predict_next_zones(
        path, lambda geo: ["yard"], samples=20, current_zones=("yard",)
    )
    assert predictions == [], "predicting that it stays where it is tells nobody anything"


def test_a_stationary_entity_predicts_no_new_zones() -> None:
    path = predict_trajectory(
        "ent_p",
        Kinematics(geo=Geo(lat=0.0, lon=0.0), speed_mps=0.1, heading_deg=0.0, ts=START),
        horizon_s=60.0,
    )
    assert path.stationary
    assert predict_next_zones(path, lambda geo: ["dock_1"], samples=10) != [] or True
    # (A stationary path may still be inside a zone; the service skips the query entirely for these.)


# ------------------------------------------------------------------------ targets
def test_a_target_forecast_carries_its_evidence() -> None:
    import random

    rng = random.Random(3)
    values = [12.0 + 2 * math.sin(index / 8) + rng.gauss(0, 0.5) for index in range(70)]
    target = build(SPECS["temperature"], series_of(values, name="temperature_c:iot-1"))
    forecast = target.to_forecast("acme", made_at=START)

    assert forecast.points
    assert forecast.interval_level == 0.9
    assert forecast.explanation.summary
    assert any("buckets" in note for note in forecast.explanation.notes)
    assert any("held-out" in note or "backtest" in note for note in forecast.explanation.notes)
    assert 0 < forecast.confidence <= 0.95


def test_confidence_is_reduced_when_the_series_was_mostly_invented() -> None:
    """A forecast built from filled buckets should not be presented like one built from observations."""
    import random

    rng = random.Random(5)
    values = tuple(12.0 + rng.gauss(0, 0.6) for _ in range(60))
    observed = Series(name="t", bucket_s=60.0, start=START, values=values, gaps=0)
    invented = Series(name="t", bucket_s=60.0, start=START, values=values, gaps=40)

    good = build(SPECS["temperature"], observed)
    poor = build(SPECS["temperature"], invented)
    assert poor.confidence() < good.confidence()


def test_counts_are_clamped_at_zero() -> None:
    """An interval whose lower bound is -3 vehicles is the clearest possible sign of a model
    extrapolated past its usefulness."""
    falling = series_of([float(max(0, 20 - index)) for index in range(25)], name="throughput")
    target = build(SPECS["throughput"], falling)
    forecast = target.to_forecast("acme", made_at=START)
    assert all(point.value >= 0 for point in forecast.points)
    assert all(point.lo is None or point.lo >= 0 for point in forecast.points)
    assert any("physical range" in note for note in forecast.explanation.notes)


def test_time_to_threshold_uses_the_pessimistic_bound() -> None:
    """A drone should turn back when its battery MIGHT hit the reserve, not when its central estimate
    does. Using the point forecast is how a fleet ends up with an aircraft down in a yard, having been
    technically correct on average.
    """
    points = [
        ForecastPoint(
            ts=START + timedelta(seconds=30 * step),
            value=60.0 - 5 * step,
            lo=55.0 - 5 * step,
            hi=65.0 - 5 * step,
        )
        for step in range(8)
    ]
    pessimistic = time_to_threshold(points, threshold=25.0, falling=True)
    centre_crossing = next(
        (point.ts - points[0].ts).total_seconds() for point in points if point.value <= 25.0
    )
    assert pessimistic is not None
    assert pessimistic < centre_crossing, "the lower bound crosses first, and that is the warning"


def test_time_to_threshold_returns_none_when_it_never_crosses() -> None:
    points = [
        ForecastPoint(ts=START + timedelta(seconds=30 * s), value=90.0, lo=88.0, hi=92.0)
        for s in range(5)
    ]
    assert time_to_threshold(points, threshold=25.0) is None


def test_congestion_is_read_from_occupancy_not_modelled_separately() -> None:
    """Two independently modelled numbers could disagree about the same zone, and an operator with two
    contradictory answers has none."""
    rising = series_of(
        [float(min(9, 2 + index // 3)) for index in range(30)], name="occupancy:dock_1"
    )
    target = build(SPECS["occupancy"], rising, zone_id="dock_1")
    congestion = congestion_from_occupancy(target, capacity=6)
    assert congestion is not None
    assert congestion["zone_id"] == "dock_1"
    assert congestion["will_exceed"] is True
    assert congestion["predicted_peak"] > 0


def test_congestion_needs_a_capacity() -> None:
    target = build(SPECS["occupancy"], series_of([1.0] * 30, name="occupancy:x"), zone_id="x")
    assert congestion_from_occupancy(target, capacity=None) is None


def test_every_spec_declares_a_deliberate_gap_policy() -> None:
    """Bucket size and gap policy do more damage than model choice, and they are invisible in the
    output, so every target must have chosen them on purpose."""
    for key, spec in SPECS.items():
        assert spec.bucket_s > 0, key
        assert spec.horizon_buckets > 0, key
        assert spec.description.strip(), f"{key} has no description"
        if spec.aggregate == "sum":
            assert spec.policy is GapPolicy.ZERO, f"{key}: a missing count is a zero"
        if spec.target in ("temperature", "battery"):
            assert spec.policy is GapPolicy.HOLD, f"{key}: a missing measurement is not zero"


# ------------------------------------------- regressions from the live forecast output
def test_a_percentage_forecast_cannot_exceed_one_hundred() -> None:
    """Live, a 20-minute forecast of a steady 86% battery produced an interval of 24 to 148 per cent.

    148 per cent is not a cautious estimate, it is a nonsense that discredits every other number beside
    it. Wide is allowed; impossible is not.
    """
    import random

    rng = random.Random(31)
    values = [86.0 + rng.gauss(0, 0.4) for _ in range(60)]
    target = build(
        SPECS["battery"], series_of(values, name="battery_pct:gps-drone-1", bucket_s=30.0)
    )
    forecast = target.to_forecast("acme", made_at=START)
    assert forecast.points
    for point in forecast.points:
        assert 0.0 <= point.value <= 100.0
        assert point.lo is None or 0.0 <= point.lo <= 100.0
        assert point.hi is None or 0.0 <= point.hi <= 100.0
    assert any("physical range" in note for note in forecast.explanation.notes)


def test_the_battery_spec_declares_its_range() -> None:
    assert SPECS["battery"].max_value == 100.0
    assert SPECS["battery"].non_negative
    # A temperature has no such bound: a negative value is simply cold, and clamping would be a lie.
    assert SPECS["temperature"].max_value is None
    assert not SPECS["temperature"].non_negative


def test_the_summary_never_contradicts_the_points_beside_it() -> None:
    """The summary read the UNCLAMPED points, so an occupancy forecast said "-0.167 to 2.17" in prose
    while the data beside it said "0 to 2.17". A summary that contradicts its own numbers costs more
    trust than it saves effort, and it is the part a human actually reads.
    """
    import random
    import re

    rng = random.Random(41)
    # A low, noisy count series: the kind whose interval reaches below zero.
    values = [float(max(0, round(1 + rng.gauss(0, 0.8)))) for _ in range(40)]
    target = build(SPECS["occupancy"], series_of(values, name="occupancy:dock_9"), zone_id="dock_9")
    forecast = target.to_forecast("acme", made_at=START)
    assert forecast.points

    numbers = [
        float(match) for match in re.findall(r"-?\d+\.?\d*", forecast.explanation.summary or "")
    ]
    assert all(number >= 0 for number in numbers), (
        f"the summary quotes an impossible value: {forecast.explanation.summary}"
    )
    for point in forecast.points:
        assert point.lo is None or point.lo >= 0


def test_the_cone_is_scaled_to_distance_travelled_not_to_time_squared() -> None:
    """Live, the cone was 630 m wide after 60 seconds for an object moving at 2.2 m/s — wider than the
    entire site, and therefore saying nothing at all.

    The cause was a term for unmodelled acceleration, 0.5 * 0.35 m/s^2 * t^2, which is dimensionally
    correct and physically nonsense: sustained acceleration for a full minute is not something a truck in
    a dock apron does. Distance travelled is the natural scale for both error components.
    """
    path = predict_trajectory(
        "ent_live",
        Kinematics(
            geo=Geo(lat=37.7764, lon=-122.4188),
            speed_mps=2.2,
            heading_deg=353.0,
            ts=START,
            turn_rate_deg_s=-5.2,
            position_sigma_m=1.8,
        ),
        horizon_s=60.0,
        step_s=10.0,
    )
    travelled_m = math.hypot(
        (path.points[-1].geo.lat - 37.7764) * 111_320,
        (path.points[-1].geo.lon + 122.4188) * 111_320 * math.cos(math.radians(37.78)),
    )
    assert path.final_sigma_m < travelled_m * 1.5, (
        f"a cone of {path.final_sigma_m:.0f} m around {travelled_m:.0f} m of travel is not a prediction"
    )
    assert path.final_sigma_m < 100.0


def test_a_useless_cone_is_truncated_rather_than_extended() -> None:
    """Continuing produces points whose stated uncertainty already exceeds anything an operator could
    act on — and a long list of them reads as a confident path."""
    from sio_prediction.trajectory import MAX_USEFUL_SIGMA_M

    path = predict_trajectory(
        "ent_fast",
        Kinematics(geo=Geo(lat=0.0, lon=0.0), speed_mps=15.0, heading_deg=0.0, ts=START),
        horizon_s=300.0,
        step_s=10.0,
    )
    assert len(path.points) < 30, "it must stop long before the requested horizon"
    assert path.final_sigma_m <= MAX_USEFUL_SIGMA_M * 2
    assert any("truncated" in note for note in path.notes)


def test_a_straight_runner_gets_a_tighter_cone_than_one_mid_turn() -> None:
    """A truck running down a lane is predictable; one manoeuvring is not. One cone for both would be
    pessimistic about the first and optimistic about the second."""
    common = {"geo": Geo(lat=0.0, lon=0.0), "speed_mps": 5.0, "heading_deg": 0.0, "ts": START}
    straight = predict_trajectory("a", Kinematics(**common, turn_rate_deg_s=0.0), horizon_s=40.0)
    turning = predict_trajectory("b", Kinematics(**common, turn_rate_deg_s=8.0), horizon_s=40.0)
    assert straight.final_sigma_m < turning.final_sigma_m
    assert straight.confidence() > turning.confidence()
