"""The provisioned dashboards must reference metrics that exist (PRD M20, Tier 2 #4).

This file exists because I wrote a seven-panel dashboard in which **six panels referenced metrics the platform
does not export**. Every name was plausible — `sio_messages_published_total`, `sio_frames_inferred_total`,
`sio_http_request_duration_seconds_bucket` — and every one was wrong, because I wrote the panels from what a
pipeline dashboard *ought* to show rather than from what this pipeline actually publishes.

The failure mode is nasty: a provisioned dashboard of empty panels does not error. It renders, it looks
plausible, and a reviewer opening it concludes the pipeline is dead. That is worse than a broken dashboard,
because a broken one gets fixed.

So the check is mechanical: extract every `sio_*` name from every panel expression and assert each corresponds
to an instrument in `sio_core.telemetry.Metrics`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
GRAFANA = ROOT / "infra" / "grafana"
DASHBOARDS = sorted((GRAFANA / "dashboards").glob("*.json"))

#: Suffixes Prometheus adds to a histogram or counter when it is exported.
#:
#: A dashboard legitimately references `sio_http_seconds_bucket` for a metric declared as `sio_http_seconds`,
#: so the comparison has to strip these before matching — otherwise every histogram panel would fail this test
#: and the test would be deleted rather than the dashboard fixed.
EXPORT_SUFFIXES = ("_bucket", "_count", "_sum", "_total", "_created")


def declared_metrics() -> set[str]:
    """Metric names the platform actually exports, read from the Metrics class.

    Read from a real instance rather than parsed out of the source, so a metric that exists only in a comment
    does not count as declared.
    """
    from sio_core.telemetry import Metrics

    metrics = Metrics("test")
    names: set[str] = set()
    for attribute in vars(metrics).values():
        name = getattr(attribute, "_name", None)
        if isinstance(name, str) and name.startswith("sio_"):
            names.add(name)
    return names


def referenced_metrics(dashboard: dict) -> set[str]:
    names: set[str] = set()
    for panel in dashboard.get("panels", []):
        for target in panel.get("targets", []):
            names.update(re.findall(r"\bsio_[a-z0-9_]+", target.get("expr", "")))
    return names


def base_name(referenced: str) -> str:
    for suffix in EXPORT_SUFFIXES:
        if referenced.endswith(suffix):
            return referenced[: -len(suffix)]
    return referenced


def test_there_is_at_least_one_dashboard() -> None:
    """Guards against the rest of this file passing vacuously on an empty directory."""
    assert DASHBOARDS, "no provisioned dashboards found under infra/grafana/dashboards"


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda path: path.name)
def test_every_panel_references_a_metric_the_platform_exports(path: Path) -> None:
    """The check that would have caught six empty panels.

    A dashboard of empty panels does not error. It renders, looks plausible, and a reviewer concludes the
    pipeline is dead.
    """
    dashboard = json.loads(path.read_text())
    declared = declared_metrics()
    missing = sorted(
        name
        for name in referenced_metrics(dashboard)
        if base_name(name) not in declared and name not in declared
    )
    assert not missing, (
        f"{path.name} references metrics nothing exports: {missing}\n"
        f"declared: {sorted(declared)}\n"
        "Either add the instrument to sio_core.telemetry.Metrics or fix the panel."
    )


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda path: path.name)
def test_every_panel_has_a_description(path: Path) -> None:
    """A panel title says what is plotted; a description says what to conclude from it.

    "Consumer lag" is a label. "A rising lag means a consumer cannot keep up, and it rises long before anything
    errors" is the reason the panel is on the dashboard, and without it the panel is decoration.
    """
    dashboard = json.loads(path.read_text())
    undescribed = [
        panel.get("title", "(untitled)")
        for panel in dashboard.get("panels", [])
        if not panel.get("description")
    ]
    assert not undescribed, f"{path.name}: panels with no description: {undescribed}"


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda path: path.name)
def test_no_panel_plots_a_mean_latency(path: Path) -> None:
    """Latency has a long right tail: a mean sits above most requests while hiding the slow ones.

    Asserted rather than trusted, because `avg(rate(..._sum))/rate(..._count)` is the first thing anybody
    writes when adding a latency panel, and it is wrong every time.
    """
    dashboard = json.loads(path.read_text())
    offenders = [
        f"{panel.get('title')}: {target.get('expr')}"
        for panel in dashboard.get("panels", [])
        for target in panel.get("targets", [])
        if "_sum" in target.get("expr", "") and "_count" in target.get("expr", "")
    ]
    assert not offenders, (
        "these panels compute a mean latency, which describes almost no request:\n"
        + "\n".join(offenders)
    )


def test_dashboards_are_not_editable_in_the_ui() -> None:
    """A provisioned dashboard that drifts from its file is a hand-made one nobody can reproduce."""
    provider = yaml.safe_load((GRAFANA / "provisioning" / "dashboards" / "sio.yaml").read_text())
    for entry in provider["providers"]:
        assert entry.get("allowUiUpdates") is False, (
            f"{entry['name']} allows UI updates, so a dashboard can silently diverge from the repo"
        )
    for path in DASHBOARDS:
        assert json.loads(path.read_text()).get("editable") is False


def test_the_datasources_provision_both_prometheus_and_postgres() -> None:
    """Two, and the split is deliberate.

    Prometheus answers "what is the pipeline doing"; Postgres answers questions that need the actual records.
    A tile showing "alerts by severity" should read the alerts table, not a counter that resets on restart.
    """
    sources = yaml.safe_load((GRAFANA / "provisioning" / "datasources" / "sio.yaml").read_text())
    kinds = {entry["type"] for entry in sources["datasources"]}
    assert kinds == {"prometheus", "postgres"}


def test_the_metrics_the_plan_names_all_exist() -> None:
    """The plan names five dashboards: throughput, consumer lag, detection FPS, event rates, API latency.

    Each needs an instrument, and `sio_http_seconds` was genuinely missing until this exercise — the other
    four existed under names I had guessed wrongly. Worth separating those two failures: four were my
    mistake, one was a real gap.
    """
    declared = declared_metrics()
    for needed in (
        "sio_messages_produced_total",  # throughput
        "sio_consumer_lag",  # consumer lag
        "sio_inference_seconds",  # detection FPS
        "sio_dead_lettered_total",  # pipeline health
        "sio_http_seconds",  # API latency — added for this
        "sio_up",
    ):
        # Through `base_name`, because these are the EXPORTED names a dashboard author writes while `declared`
        # holds the base names prometheus_client stores. Comparing them directly is how this test failed on
        # its first run, and the fix belongs here rather than in the list — a panel is written against
        # `sio_messages_produced_total`, so that is the name worth asserting.
        assert base_name(needed) in declared, (
            f"{needed} is not exported, so a dashboard panel for it would be empty"
        )


def test_route_labels_use_the_template_not_the_path() -> None:
    """Unbounded label cardinality exhausts the scraper, not the service.

    `/api/alerts/alt_01K...` as a label value creates a time series per alert id, and the symptom appears in
    Prometheus's memory rather than anywhere the platform is being watched. The middleware must use the matched
    route template.
    """
    from sio_core.service import _route_label

    class Scoped:
        def __init__(self, route: object | None) -> None:
            self.scope = {"route": route}

    class Route:
        path = "/api/alerts/{alert_id}"

    assert _route_label(Scoped(Route())) == "/api/alerts/{alert_id}"
    # An unmatched request gets one bucket, so a scanner probing random URLs cannot create series.
    assert _route_label(Scoped(None)) == "unmatched"
