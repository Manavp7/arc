"""Analytics (PRD M19, Phase 6).

Two themes, and they are the two ways an analytics layer misleads people.

**A summary statistic that hides the shape of its data.** A mean dwell time of 18 minutes describes a yard where
every truck takes 18 minutes and a yard where half take 4 and half take 32 identically — and those are different
sites with different problems. Most of these tests check that the shape is *named*, not just that the arithmetic
is right.

**A heatmap that is really a movement record.** Aggregating on the server is a privacy control, not a rendering
optimisation, and the suppression threshold is what makes it one. A hexagon containing one person is not an
aggregate.
"""

from __future__ import annotations

import pytest
from sio_analytics.heatmap import DISPLAY_RESOLUTION, MIN_CELL_COUNT, aggregate, edge_length_m
from sio_analytics.kpis import (
    DWELL_BUCKETS_MIN,
    PERCENTILES,
    RISK_WEIGHTS,
    risk_index,
    summarise,
    utilisation,
)
from sio_analytics.service import render_report


def dwell(values: list[float]):
    return summarise("dwell", "minutes", values, DWELL_BUCKETS_MIN)


# --- shape, which is the point -------------------------------------------------------------------
def test_a_bimodal_distribution_says_so() -> None:
    """The case a mean actively hides.

    Two populations sharing one queue — pass-through traffic and long stays — is a real and common yard
    condition with a specific remedy. A mean of 25 minutes describes neither group and points at nothing.
    """
    result = dwell([2, 3, 3, 4, 4, 5, 4, 3, 45, 48, 52, 55, 60, 47, 51])
    assert "bimodal" in result.shape
    assert "two populations sharing one queue" in result.shape
    # And the mean is genuinely useless here, which is why the sentence matters.
    assert result.percentiles["p50"] < 10
    assert result.mean > 20


def test_a_long_tailed_distribution_says_the_mean_describes_almost_nobody() -> None:
    result = dwell([5, 6, 5, 7, 6, 5, 8, 6, 7, 5, 120, 180])
    assert "long right tail" in result.shape
    assert "describes almost nobody" in result.shape


def test_a_symmetric_distribution_says_the_mean_is_fair() -> None:
    """The fix must not label everything pathological.

    A report that always warns is a report nobody reads, so the symmetric case has to be recognised too.
    """
    result = dwell([18, 19, 20, 20, 21, 22, 20, 19, 21, 20])
    assert "symmetric" in result.shape
    assert "mean is a fair summary" in result.shape


def test_too_few_measurements_is_admitted_rather_than_described() -> None:
    """Naming a shape from five points is a guess dressed as a finding."""
    result = dwell([5, 40, 6, 38, 7])
    assert "too few" in result.shape


def test_an_empty_window_is_not_an_error() -> None:
    result = dwell([])
    assert result.count == 0
    assert "no measurements" in result.shape
    assert result.histogram == []


# --- percentiles ---------------------------------------------------------------------------------
def test_percentiles_interpolate_rather_than_pick_the_nearest_rank() -> None:
    """Nearest-rank makes p95 and p99 identical on small samples.

    A report where two percentiles always agree teaches the reader to ignore both.
    """
    result = dwell([float(value) for value in range(1, 21)])
    assert result.percentiles["p95"] != result.percentiles["p99"]
    assert result.percentiles["p50"] == pytest.approx(10.5)


def test_percentiles_are_ordered() -> None:
    result = dwell([float(value) for value in range(1, 101)])
    values = [result.percentiles[f"p{p}"] for p in PERCENTILES]
    assert values == sorted(values)


def test_a_single_measurement_does_not_divide_by_zero() -> None:
    result = dwell([12.0])
    assert result.percentiles["p50"] == 12.0
    assert result.percentiles["p99"] == 12.0


# --- histogram buckets ---------------------------------------------------------------------------
def test_bucket_edges_match_how_a_yard_is_discussed() -> None:
    """Chosen rather than computed.

    Equal-width buckets over the observed range would put every boundary somewhere nobody cares about; "under
    five minutes" and "over an hour" are the phrases operators actually use.
    """
    assert 5 in DWELL_BUCKETS_MIN
    assert 60 in DWELL_BUCKETS_MIN
    assert list(DWELL_BUCKETS_MIN) == sorted(DWELL_BUCKETS_MIN)
    # These are UPPER bounds, not edges including zero. A leading 0 makes the first bucket "value < 0", which
    # is empty by construction — and it put a "0 to 0: 0 visits (0%)" row in every single generated report.
    assert DWELL_BUCKETS_MIN[0] > 0


def test_no_histogram_bucket_is_degenerate() -> None:
    """A "0 to 0" row is wrong in a way that costs more than it looks.

    It appeared in every report from the live stack, and a degenerate row in a user-visible table makes a
    reader distrust the numbers beside it — which are correct.
    """
    for values in ([0.5], [1.0, 2.0, 3.0], [float(v) for v in range(1, 300, 7)], [1000.0]):
        result = dwell(values)
        for bucket in result.histogram:
            assert bucket["from"] != bucket["to"], f"degenerate bucket {bucket} for {values[:3]}"


def test_the_histogram_accounts_for_every_measurement() -> None:
    """A histogram that loses rows is worse than no histogram — the shares look plausible and are wrong."""
    values = [1.0, 4.0, 10.0, 45.0, 90.0, 300.0, 1000.0]
    result = dwell(values)
    assert sum(bucket["count"] for bucket in result.histogram) == len(values)
    assert sum(bucket["share"] for bucket in result.histogram) == pytest.approx(1.0, abs=0.01)


def test_the_last_bucket_is_open_ended() -> None:
    """Otherwise a four-hour dwell has nowhere to go and silently vanishes."""
    result = dwell([1000.0, 2000.0, 3000.0])
    assert result.histogram[-1]["to"] is None
    assert result.histogram[-1]["count"] == 3


# --- risk index ----------------------------------------------------------------------------------
def test_the_risk_index_shows_every_term() -> None:
    """A single score will end up on a wall. One that cannot be decomposed invites an argument nobody can
    settle."""
    index = risk_index(
        open_criticals=2,
        open_alerts=40,
        unacknowledged=38,
        zones_total=17,
        zones_uncovered=4,
        restricted_occupied=1,
        restricted_total=2,
        anomalies_last_hour=6,
        events_last_hour=120,
    )
    assert set(index.terms) == set(RISK_WEIGHTS)
    for detail in index.terms.values():
        assert detail.get("why"), "a term with no explanation cannot be checked"
        assert 0.0 <= detail["normalised"] <= 1.0
    assert index.formula
    assert index.drivers


def test_risk_drivers_are_ordered_by_contribution() -> None:
    """A score of 62 tells nobody what to do; "62, mostly because 4 of 17 zones have no camera" does."""
    index = risk_index(
        open_criticals=0,
        open_alerts=10,
        unacknowledged=0,
        zones_total=20,
        zones_uncovered=20,
        restricted_occupied=0,
        restricted_total=5,
        anomalies_last_hour=0,
        events_last_hour=100,
    )
    assert index.drivers
    assert "blind spot" in index.drivers[0], "the dominant term must lead"


def test_risk_is_bounded_and_normalised_by_site_size() -> None:
    """An absolute count would make a large site permanently high-risk for being large."""
    small = risk_index(
        open_criticals=0,
        open_alerts=2,
        unacknowledged=1,
        zones_total=3,
        zones_uncovered=1,
        restricted_occupied=0,
        restricted_total=1,
        anomalies_last_hour=0,
        events_last_hour=10,
    )
    large = risk_index(
        open_criticals=0,
        open_alerts=20,
        unacknowledged=10,
        zones_total=30,
        zones_uncovered=10,
        restricted_occupied=0,
        restricted_total=10,
        anomalies_last_hour=0,
        events_last_hour=100,
    )
    assert small.score == pytest.approx(large.score, abs=1.0), (
        "the same proportions on a small and a large site should score the same"
    )
    assert 0 <= small.score <= 100


def test_a_quiet_site_scores_zero_and_says_why() -> None:
    index = risk_index(
        open_criticals=0,
        open_alerts=0,
        unacknowledged=0,
        zones_total=10,
        zones_uncovered=0,
        restricted_occupied=0,
        restricted_total=2,
        anomalies_last_hour=0,
        events_last_hour=50,
    )
    assert index.score == 0.0
    assert index.band == "low"
    assert "nothing is contributing materially" in index.drivers[0]


def test_saturating_terms_do_not_overflow_the_scale() -> None:
    """Ten criticals is not worse than three in a way anybody acts on differently — both mean "look now"."""
    three = risk_index(
        open_criticals=3,
        open_alerts=3,
        unacknowledged=3,
        zones_total=1,
        zones_uncovered=1,
        restricted_occupied=1,
        restricted_total=1,
        anomalies_last_hour=100,
        events_last_hour=100,
    )
    ten = risk_index(
        open_criticals=10,
        open_alerts=3,
        unacknowledged=3,
        zones_total=1,
        zones_uncovered=1,
        restricted_occupied=1,
        restricted_total=1,
        anomalies_last_hour=100,
        events_last_hour=100,
    )
    assert three.score == ten.score
    assert three.score <= 100.0


# --- utilisation ---------------------------------------------------------------------------------
def test_utilisation_is_clamped() -> None:
    """Overlapping intervals can sum past the window, and 101% reads as a broken dashboard."""
    assert utilisation(busy_seconds=7200, window_seconds=3600) == 1.0
    assert utilisation(busy_seconds=1800, window_seconds=3600) == 0.5
    assert utilisation(busy_seconds=-5, window_seconds=3600) == 0.0
    assert utilisation(busy_seconds=100, window_seconds=0) == 0.0


# --- heatmap: a privacy control ------------------------------------------------------------------
def a_crowd(lat: float, lon: float, entities: int, observations_each: int = 5) -> list[dict]:
    return [
        {"lat": lat, "lon": lon, "entity_id": f"e{index}", "type": "truck", "zone_id": "dock_1"}
        for index in range(entities)
        for _ in range(observations_each)
    ]


def test_a_cell_with_one_person_is_suppressed() -> None:
    """The whole reason server-side aggregation is a privacy control.

    A hexagon containing one person is not an aggregate, it is that person's location — and shipping it to a
    browser hands out a movement record the API can no longer redact.
    """
    positions = a_crowd(37.7764, -122.4189, entities=8)
    positions.append(
        {"lat": 37.79, "lon": -122.40, "entity_id": "lonely", "type": "person", "zone_id": None}
    )
    result = aggregate(positions).describe()
    assert len(result["cells"]) == 1
    assert result["suppressed"]["cells"] == 1
    assert "not an aggregate" in result["suppressed"]["why"]
    # And the suppressed entity does not appear anywhere in the payload.
    assert "lonely" not in str(result)


def test_suppression_counts_distinct_entities_not_observations() -> None:
    """A hundred observations of one parked truck is still one truck.

    Counting observations would let a stationary vehicle unlock a cell that discloses only itself, which is the
    exact failure the threshold exists to prevent.
    """
    parked = [
        {"lat": 37.7764, "lon": -122.4189, "entity_id": "one_truck", "type": "truck"}
        for _ in range(200)
    ]
    result = aggregate(parked).describe()
    assert result["cells"] == []
    assert result["suppressed"]["cells"] == 1
    assert result["suppressed"]["observations"] == 200


def test_suppression_is_reported_not_silent() -> None:
    """A heatmap that quietly drops 40% of its data looks like a quiet site."""
    positions = a_crowd(37.7764, -122.4189, entities=8)
    for index in range(6):
        positions.append(
            {
                "lat": 37.80 + index * 0.01,
                "lon": -122.40,
                "entity_id": f"solo{index}",
                "type": "person",
            }
        )
    result = aggregate(positions).describe()
    assert result["suppressed"]["cells"] == 6
    assert result["total_observations"] == len(positions)


def test_the_threshold_is_the_conventional_small_cell_floor() -> None:
    assert MIN_CELL_COUNT == 5


def test_display_resolution_is_coarser_than_indexing_resolution() -> None:
    """Indexing precision and display precision are different questions.

    Resolution 12 is ~9 m, finer than any position here is accurate, and produces a heatmap of measurement
    noise. Resolution 11 is ~25 m — roughly a truck bay.
    """
    from sio_core import get_settings

    assert get_settings().h3_resolution > DISPLAY_RESOLUTION
    assert 20 < edge_length_m(DISPLAY_RESOLUTION) < 30


def test_cells_are_ordered_busiest_first() -> None:
    """So a client rendering a subset renders the part that matters."""
    positions = a_crowd(37.7764, -122.4189, entities=8, observations_each=10)
    positions += a_crowd(37.7900, -122.4300, entities=6, observations_each=2)
    result = aggregate(positions).describe()
    counts = [cell["observations"] for cell in result["cells"]]
    assert counts == sorted(counts, reverse=True)


def test_positions_without_coordinates_are_skipped_not_fatal() -> None:
    positions = a_crowd(37.7764, -122.4189, entities=8)
    positions.append({"entity_id": "nowhere", "type": "truck"})
    result = aggregate(positions).describe()
    assert len(result["cells"]) == 1


# --- the report ----------------------------------------------------------------------------------
def a_summary(**overrides) -> dict:
    data = {
        "window_hours": 24,
        "generated_at": "2026-07-25T12:00:00Z",
        "counts": {"entities": 33, "events": 1240, "open_alerts": 40, "pending_decisions": 7},
        "dwell": {
            "overall": dwell([2, 3, 3, 4, 4, 5, 4, 3, 45, 48, 52, 55, 60, 47, 51]).describe(),
            "open_visits_excluded": 12,
        },
        "throughput": {
            "totals": {"dock_1": 88, "gate_a": 54},
            "entries_per_hour": 5.9,
            "smoothing": "none — a spiky chart is a true chart of a spiky yard",
        },
        "utilisation": {"zones": [{"zone_id": "dock_1", "visits": 22, "utilisation": 0.61}]},
        "risk": risk_index(
            open_criticals=2,
            open_alerts=40,
            unacknowledged=38,
            zones_total=17,
            zones_uncovered=4,
            restricted_occupied=1,
            restricted_total=2,
            anomalies_last_hour=6,
            events_last_hour=120,
        ).describe(),
    }
    data.update(overrides)
    return data


def test_the_report_leads_with_what_is_wrong_not_with_the_formula() -> None:
    """A reader wants to know what is wrong before how it was arithmetically arrived at."""
    report = render_report(a_summary())
    assert report.index("unacknowledged") < report.index("Formula")


def test_the_report_carries_the_distribution_shape() -> None:
    """The part a reader cannot get by glancing at a chart."""
    report = render_report(a_summary())
    assert "bimodal" in report


def test_the_report_says_what_it_excluded() -> None:
    """12 open visits omitted from a distribution of 15 changes what the distribution means."""
    report = render_report(a_summary())
    assert "12 visit(s) are still in progress" in report
    assert "depend on when the report was run" in report


def test_the_report_states_that_nothing_is_read_from_a_counter() -> None:
    """The claim that makes the report reproducible, said where a reader will see it."""
    report = render_report(a_summary())
    assert "regenerated for any past window" in report
    assert "Nothing is read from a counter" in report


def test_an_empty_report_still_renders() -> None:
    """A dashboard on a fresh install must not 500.

    "No completed visits in this window" is an answer; a stack trace is not.
    """
    empty = a_summary(
        counts={"entities": 0, "events": 0, "open_alerts": 0, "pending_decisions": 0},
        dwell={"overall": dwell([]).describe(), "open_visits_excluded": 0},
        throughput={"totals": {}, "entries_per_hour": 0.0, "smoothing": "none"},
        utilisation={"zones": []},
    )
    report = render_report(empty)
    assert "No completed visits" in report
    assert "No zone entries recorded" in report
    assert "No zone occupancy recorded" in report


def test_the_report_is_markdown_a_reader_can_paste_anywhere() -> None:
    """Markdown rather than PDF: it diffs, it pastes into a ticket, anything can turn it into a PDF."""
    report = render_report(a_summary())
    assert report.startswith("# Site report")
    assert "| from (min) |" in report, "the histogram should be a table, not prose"
    assert report.endswith("\n")
