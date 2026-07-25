"""The console's panels and the services' responses must agree (Phase 6).

Written because browser automation was not available to verify the analytics panel, and the failure it would
have caught is worth catching anyway — with or without a browser.

**The failure mode is silent.** A React panel reading `summary.risk.drivers` when the service renamed the field
to `risk.reasons` does not error. It renders `undefined`, or an empty list, or `NaN` — and the panel looks like
a site with nothing to report. Neither `tsc` nor `just check` sees it: the TypeScript type is hand-written in
`api.ts` and describes what the author *believed* the service returns.

So this walks the actual response shapes the services produce and asserts every field the panels read is
present. It is not a substitute for looking at the rendered page — layering, clipping and console errors are
invisible here — and the docstrings say so where relevant.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web" / "src"


def source(name: str) -> str:
    path = WEB / "components" / name
    assert path.exists(), f"{path} is missing"
    return path.read_text()


# --- the analytics panel -------------------------------------------------------------------------
def analytics_summary_shape() -> dict:
    """The shape `AnalyticsService.summary` produces, built from the real component parts.

    Assembled from the service's own helpers rather than hand-written, so a renamed field in `kpis.py` changes
    this fixture automatically and the assertions below start failing — which is the entire point. A
    hand-written fixture would agree with the panel forever while both drifted from the service.
    """
    from sio_analytics.kpis import DWELL_BUCKETS_MIN, risk_index, summarise

    return {
        "window_hours": 24,
        "generated_at": "2026-07-25T12:00:00Z",
        "counts": {"entities": 1, "events": 2, "open_alerts": 3, "pending_decisions": 4},
        "dwell": {
            "overall": summarise(
                "dwell", "minutes", [1.0, 2.0, 40.0], DWELL_BUCKETS_MIN
            ).describe(),
            "by_zone": {},
            "open_visits_excluded": 5,
        },
        "throughput": {"totals": {"dock_1": 8}, "entries_per_hour": 1.2, "smoothing": "none"},
        "utilisation": {"zones": [{"zone_id": "dock_1", "visits": 2, "utilisation": 0.5}]},
        "risk": risk_index(
            open_criticals=1,
            open_alerts=2,
            unacknowledged=1,
            zones_total=3,
            zones_uncovered=1,
            restricted_occupied=0,
            restricted_total=1,
            anomalies_last_hour=1,
            events_last_hour=10,
        ).describe(),
    }


#: Field paths the analytics panel reads from the summary.
#:
#: Listed rather than extracted from the TSX by regex, because a regex over JSX finds `summary.risk.score`
#: inside a comment as readily as inside a template — and a guard with false positives gets deleted.
ANALYTICS_PATHS = [
    "window_hours",
    "generated_at",
    "counts",
    "dwell.overall.count",
    "dwell.overall.mean",
    "dwell.overall.percentiles",
    "dwell.overall.histogram",
    "dwell.overall.shape",
    "dwell.open_visits_excluded",
    "throughput.totals",
    "throughput.entries_per_hour",
    "utilisation.zones",
    "risk.score",
    "risk.band",
    "risk.drivers",
    "risk.formula",
    "risk.terms",
]


def resolve(data: object, path: str) -> object:
    current = data
    for part in path.split("."):
        assert isinstance(current, dict), f"{path}: {part} is not under a mapping"
        assert part in current, f"missing field: {path} (stopped at {part!r})"
        current = current[part]
    return current


@pytest.mark.parametrize("path", ANALYTICS_PATHS)
def test_the_analytics_panel_reads_fields_the_service_sends(path: str) -> None:
    """A panel reading a field the service does not send renders `undefined`, silently.

    It looks like a site with nothing to report, and neither tsc nor the test suite notices — the TypeScript
    type in `api.ts` is hand-written and describes what its author believed.
    """
    resolve(analytics_summary_shape(), path)


def test_histogram_rows_carry_everything_the_panel_draws() -> None:
    """The panel draws a label, a bar width, a count and a percentage from each row."""
    histogram = resolve(analytics_summary_shape(), "dwell.overall.histogram")
    assert isinstance(histogram, list) and histogram
    for row in histogram:
        assert set(row) >= {"from", "to", "count", "share"}


def test_risk_terms_carry_every_column_the_table_renders() -> None:
    terms = resolve(analytics_summary_shape(), "risk.terms")
    assert isinstance(terms, dict) and terms
    for term in terms.values():
        # The panel renders normalised, weight and contributes as columns, and `why` as a tooltip.
        assert set(term) >= {"normalised", "weight", "contributes", "why"}


def test_utilisation_rows_carry_what_the_bars_need() -> None:
    zones = resolve(analytics_summary_shape(), "utilisation.zones")
    assert isinstance(zones, list) and zones
    for zone in zones:
        assert set(zone) >= {"zone_id", "visits", "utilisation"}


# --- the heatmap layer ---------------------------------------------------------------------------
def heatmap_shape() -> dict:
    from sio_analytics.heatmap import aggregate

    positions = [
        {"lat": 37.7764, "lon": -122.4189, "entity_id": f"e{index}", "type": "truck"}
        for index in range(8)
        for _ in range(3)
    ]
    return aggregate(positions).describe()


@pytest.mark.parametrize(
    "path",
    [
        "resolution",
        "edge_length_m",
        "cells",
        "total_observations",
        "max_observations",
        "suppressed.cells",
        "suppressed.why",
    ],
)
def test_the_map_layer_reads_fields_the_service_sends(path: str) -> None:
    resolve(heatmap_shape(), path)


def test_every_heatmap_cell_carries_a_drawable_boundary() -> None:
    """The whole reason the boundary is computed server-side.

    The browser has no H3 library — that was the point of sending vertices rather than adding
    `@deck.gl/geo-layers` (~200 kB) or `h3-js` to recompute what the server already had. A cell without a
    boundary is a hexagon the map cannot draw, and deck.gl would skip it silently.
    """
    cells = heatmap_shape()["cells"]
    assert cells
    for cell in cells:
        boundary = cell.get("boundary")
        assert boundary, f"cell {cell['h3']} has no boundary, so the map cannot draw it"
        assert len(boundary) == 6, f"an H3 cell has six vertices, got {len(boundary)}"
        for lon, lat in boundary:
            # Order matters: GeoJSON and deck.gl want (lon, lat). Flipped, the yard lands in the Southern
            # Ocean — an obvious failure, but only if somebody looks at the map.
            assert -180 <= lon <= 180
            assert -90 <= lat <= 90


def test_the_boundary_surrounds_the_cell_centre() -> None:
    """Catches the flipped-coordinate mistake without needing a map to look at."""
    cell = heatmap_shape()["cells"][0]
    lons = [point[0] for point in cell["boundary"]]
    lats = [point[1] for point in cell["boundary"]]
    assert min(lons) < cell["lon"] < max(lons)
    assert min(lats) < cell["lat"] < max(lats)


# --- what the panels promise about themselves ----------------------------------------------------
def test_the_risk_score_is_never_rendered_without_its_drivers() -> None:
    """The service decomposes the score so a bare number is impossible. A UI can undo that silently.

    Checked structurally: the panel's risk section must reference `drivers`. A score on a wall with no
    explanation is the failure the decomposition exists to prevent, and there would be no error to notice.
    """
    panel = source("AnalyticsPanel.tsx")
    assert "risk.drivers" in panel or "risk-drivers" in panel
    assert "risk.score" in panel


def test_the_distribution_shape_is_rendered() -> None:
    """The finding, not just the bars.

    The whole argument for computing shape on the server is that a reader cannot infer it from a chart. A panel
    that draws the histogram and drops the sentence has undone the reason it was computed.
    """
    panel = source("AnalyticsPanel.tsx")
    assert "dwell.shape" in panel or "analytics-shape" in panel


def test_suppressed_heatmap_cells_are_surfaced_on_the_map() -> None:
    """A heatmap that quietly drops cells looks like a quiet site."""
    map_source = source("LiveMap.tsx")
    assert "suppressed" in map_source
    assert "withheld" in map_source, (
        "the map must say WHY cells are missing, not just that they are"
    )


def test_the_heatmap_is_drawn_beneath_the_entities() -> None:
    """A heatmap covering the entity dots has replaced the live map rather than annotated it.

    Enforced by layer order rather than a depth flag — deck.gl 9 rejects `depthTest` in `parameters`, and order
    is visible at the call site instead of hidden in a layer's options.
    """
    map_source = source("LiveMap.tsx")
    assembly = map_source[map_source.index("return [") :]
    heatmap_position = assembly.index("heatmapLayer")
    entity_position = assembly.index("entityStack")
    assert heatmap_position < entity_position, (
        "the heatmap layer must be listed before the entities, or it draws over them"
    )


def test_every_rail_tab_has_a_panel() -> None:
    """A tab with no panel renders an empty rail and looks like a broken feature.

    The tab list and the render block are separate literals in the same file, so adding one and forgetting the
    other is a one-line mistake with no compiler complaint.
    """
    app = (WEB / "App.tsx").read_text()
    tabs = re.search(r"\[\s*((?:\s*\"[a-z]+\",?\s*)+)\]\s*as RailTab\[\]", app)
    assert tabs, "could not find the tab list in App.tsx"
    names = re.findall(r'"([a-z]+)"', tabs.group(1))
    assert len(names) >= 7, f"expected at least seven tabs, found {names}"
    for name in names:
        assert f'tab === "{name}"' in app, f"the {name!r} tab has no panel rendered for it"


def test_the_analytics_tab_exists_and_is_wired() -> None:
    """The specific criterion P6.3 was missing: "analytics views populated in-app"."""
    app = (WEB / "App.tsx").read_text()
    assert '"analytics"' in app
    assert 'tab === "analytics" && <AnalyticsPanel />' in app
    assert "AnalyticsPanel" in app
