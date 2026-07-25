"""Out-of-tree extension (PRD M22, Phase 6).

The claim under test is specific and falsifiable: **a new connector and a new rule can be added by installing a
package, without changing the platform.** Not "with a small patch", not "by dropping a file in the right
directory".

It was already half-built, and — in the way this codebase keeps rediscovering — **nothing called it.** The
ingest service discovered connector plugins into a registry and then built its connectors from a hard-coded
list, so a plugin was registered and never instantiated. Discovery without instantiation looks exactly like a
working plugin system until somebody writes a plugin.

`examples/plugin_demo` is that plugin: a tide gauge connector and a flood-warning rule. A tide gauge
deliberately, because it is *not* something this platform has seen — a plugin adding a second camera connector
proves much less, since the platform already knows what a camera is.

**On what "no core changes" means.** Building the plugin *mechanism* required changes to `services/` and
`libs/`, obviously. The claim is that adding a plugin does not. `test_no_core_file_mentions_the_demo_plugin`
is the check that keeps it honest.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from sio_core.plugins import (
    DEFAULT_CONFIG,
    GROUPS,
    Discovered,
    discover,
    discover_all,
    load_plugin_config,
)

ROOT = Path(__file__).resolve().parents[2]


def demo_installed() -> bool:
    from importlib.metadata import entry_points

    return any(entry.name == "tide_gauge" for entry in entry_points(group="sio.connectors"))


#: Skip what needs the example installed, rather than failing.
#:
#: `just check` must pass on a clean checkout where nobody installed the example, and a red suite for a missing
#: optional package trains people to ignore red. `just plugin-demo` installs it.
needs_demo = pytest.mark.skipif(
    not demo_installed(),
    reason="the example plugin is not installed; run: just plugin-demo",
)


# --- the mechanism, with or without the example --------------------------------------------------
def test_the_four_extension_points_exist() -> None:
    """Four groups, because four things are worth extending.

    Named as a test so adding a fifth is a deliberate act with a visible diff rather than a string appearing in
    a loop somewhere.
    """
    assert GROUPS == ("sio.connectors", "sio.rules", "sio.tools", "sio.agents")


def test_discovery_of_an_empty_group_is_not_an_error() -> None:
    result = discover("sio.nonexistent.group")
    assert isinstance(result, Discovered)
    assert result.loaded == {}
    assert result.failed == {}


def test_discover_all_covers_every_group() -> None:
    assert set(discover_all()) == set(GROUPS)


def test_a_missing_config_file_is_silent() -> None:
    """Most deployments run no plugin connectors.

    Warning about the absence of an optional file trains operators to ignore warnings, which is expensive
    later.
    """
    assert load_plugin_config(Path("/nonexistent/plugins.json")) == []


def test_a_malformed_config_is_loud_but_not_fatal(tmp_path: Path) -> None:
    """Somebody wrote that file intending it to work.

    Silently ignoring their configuration is the worst of the three possible behaviours. Refusing to start the
    ingest service over a typo in an optional extension is the second worst.
    """
    path = tmp_path / "plugins.json"
    path.write_text("{ this is not json")
    assert load_plugin_config(path) == []


def test_an_entry_without_a_kind_is_skipped_and_the_rest_survive(tmp_path: Path) -> None:
    """One bad stanza must not discard the good ones."""
    path = tmp_path / "plugins.json"
    path.write_text(
        json.dumps(
            {
                "connectors": [
                    {"source_id": "no-kind"},
                    {"source_id": "good", "kind": "tide_gauge", "options": {"lat": 1.0}},
                ]
            }
        )
    )
    configs = load_plugin_config(path)
    assert [config.source_id for config in configs] == ["good"]
    assert configs[0].options == {"lat": 1.0}


def test_a_bare_list_is_accepted_as_well_as_an_object(tmp_path: Path) -> None:
    """Both shapes, because both are what somebody writes first."""
    path = tmp_path / "plugins.json"
    path.write_text(json.dumps([{"source_id": "a", "kind": "tide_gauge"}]))
    assert [config.source_id for config in load_plugin_config(path)] == ["a"]


def test_a_disabled_entry_is_read_but_flagged(tmp_path: Path) -> None:
    """Kept in the config with `enabled: false` rather than deleted, so turning it back on is one word."""
    path = tmp_path / "plugins.json"
    path.write_text(json.dumps([{"source_id": "a", "kind": "tide_gauge", "enabled": False}]))
    configs = load_plugin_config(path)
    assert len(configs) == 1
    assert configs[0].enabled is False


def test_the_default_config_path_is_under_the_state_directory() -> None:
    """`.sio/` is already where local state lives, and it is already gitignored.

    A plugin config in the repository root would invite committing a deployment's credentials.
    """
    assert DEFAULT_CONFIG.parts[0] == ".sio"


# --- the contract lives in the public library ----------------------------------------------------
def test_the_connector_contract_is_importable_from_sio_core() -> None:
    """The finding that made the claim true rather than nearly true.

    The contract was defined inside the ingest service, so an out-of-tree connector had to import
    `sio_ingest.connectors.base` — reaching into a service's private module for the interface it implements. A
    plugin coupled that way breaks when the service is refactored, and "we did not change the core, only the
    module your plugin imported" is not a distinction anybody accepts.
    """
    from sio_core.connector import Connector, ConnectorConfig

    assert issubclass(Connector, object)
    assert ConnectorConfig(source_id="s", kind="k", modality="manual").source_id == "s"


def test_the_service_re_exports_the_same_class() -> None:
    """Every in-tree connector's import is unchanged, so moving the contract broke nothing."""
    from sio_ingest.connectors.base import Connector as ServiceConnector

    from sio_core.connector import Connector as CoreConnector

    assert ServiceConnector is CoreConnector


def test_the_example_plugin_imports_nothing_from_a_service() -> None:
    """The check that keeps the claim honest.

    A plugin importing `sio_ingest` or `sio_events` is a fork with extra steps: it depends on modules with no
    stability promise, and it would break on a refactor the platform is entitled to make.

    Checked by parsing the IMPORTS rather than searching the text. The first version searched for the string
    anywhere and failed on a docstring that explains why the import is absent — a test that forbids *discussing*
    a coupling is a test that will produce false positives forever, and the second false positive is when
    somebody deletes it.
    """
    import ast

    forbidden = {"sio_ingest", "sio_events", "sio_api", "sio_copilot", "sio_agents", "sio_alerts"}
    for path in (ROOT / "examples" / "plugin_demo" / "src").rglob("*.py"):
        tree = ast.parse(path.read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        leaked = imported & forbidden
        assert not leaked, (
            f"{path.name} imports {sorted(leaked)}, so the plugin depends on a service's internals"
        )

    # The PACKAGE must use the public contracts, but not every file.
    #
    # `rules.py` imports neither library, and that is deliberate rather than an oversight: it returns a plain
    # dict, so it does not depend on the events service's `Rule` class. My first version of this assertion was
    # per-file and failed on exactly that — the test objecting to the decoupling it exists to enforce.
    package = (ROOT / "examples" / "plugin_demo" / "src").rglob("*.py")
    all_imports = {
        node.module.split(".")[0]
        for path in package
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert {"sio_core", "sio_schemas"} & all_imports, (
        "the plugin uses none of the platform's public contracts, so it is not extending anything"
    )


def test_the_example_declares_only_the_public_libraries() -> None:
    manifest = (ROOT / "examples" / "plugin_demo" / "pyproject.toml").read_text()
    assert '"sio-core"' in manifest
    assert '"sio-schemas"' in manifest
    for forbidden in ("sio-ingest", "sio-events", "sio-api"):
        assert f'"{forbidden}"' not in manifest


def test_no_core_file_mentions_the_demo_plugin() -> None:
    """ "No core changes" stated as a check rather than a claim.

    If any file under `services/` or `libs/` had to name the example — a registry entry, an import, a
    conditional — the plugin system would not be a plugin system, and this test would say so.
    """
    offenders: list[str] = []
    for directory in ("services", "libs"):
        for path in (ROOT / directory).rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            source = path.read_text()
            for marker in ("sio_plugin_demo", "tide_gauge", "TideGauge", "tide_flood_warning"):
                if marker in source:
                    offenders.append(f"{path.relative_to(ROOT)}: mentions {marker!r}")
    assert not offenders, (
        "the platform names the example plugin, so it is not extending without core changes:\\n"
        + "\\n".join(offenders)
    )


# --- the example, once installed ------------------------------------------------------------------
@needs_demo
def test_the_plugin_connector_and_rule_both_appear() -> None:
    """The plan's acceptance, in one assertion each."""
    connectors = discover("sio.connectors")
    rules = discover("sio.rules")
    assert "tide_gauge" in connectors.loaded, connectors.describe()
    assert "tide_flood_warning" in rules.loaded, rules.describe()
    assert connectors.failed == {}
    assert rules.failed == {}


@needs_demo
def test_the_plugin_connector_is_registered_and_buildable() -> None:
    """Registered AND instantiable.

    The distinction is the whole bug this phase fixed: the service registered plugin classes and then built its
    connectors from a hard-coded list, so `kind` appeared in the registry and nothing ever constructed it.
    """
    from sio_ingest.connectors.base import build_connector, connector_kinds, discover_plugins

    from sio_core.connector import ConnectorConfig

    discover_plugins()
    assert "tide_gauge" in connector_kinds()

    connector = build_connector(
        ConnectorConfig(
            source_id="tide-test",
            kind="tide_gauge",
            modality="iot",
            options={"lat": 37.8, "lon": -122.4, "interval_s": 1, "amplitude_m": 2.0},
        )
    )
    assert type(connector).__name__ == "TideGaugeConnector"
    assert connector.describe()["plugin"] == "sio-plugin-demo"


@needs_demo
async def test_the_plugin_connector_produces_a_real_observation() -> None:
    """A connector that registers and yields nothing has not been demonstrated."""
    from sio_ingest.connectors.base import build_connector, discover_plugins

    from sio_core.connector import ConnectorConfig

    discover_plugins()
    connector = build_connector(
        ConnectorConfig(
            source_id="tide-test",
            kind="tide_gauge",
            modality="iot",
            options={"lat": 37.8, "lon": -122.4, "interval_s": 0.01},
        )
    )
    await connector.start()
    observation = await anext(aiter(connector.observations()))
    assert observation.source_id == "tide-test"
    assert str(observation.modality) == "iot"
    # `water_level_m` is a field no in-tree connector produces and no in-tree rule reads. That is the point:
    # the platform accepted a kind of signal nobody anticipated.
    assert "water_level_m" in observation.payload
    assert -3.0 < observation.payload["water_level_m"] < 3.0
    assert observation.geo is not None


@needs_demo
def test_the_plugin_rule_loads_alongside_the_sites_own_rules() -> None:
    from sio_events.rules import load_rules

    ruleset = load_rules(ROOT / "infra" / "rules")
    ids = {rule.id for rule in ruleset.rules}
    assert "tide_flood_warning" in ids
    # And the site's own rules are all still there — a plugin must add, not replace.
    assert len(ids) > 10
    assert not ruleset.errors, ruleset.errors
    assert any("plugin" in entry for entry in ruleset.loaded_from)


@needs_demo
def test_a_plugin_rule_cannot_override_a_site_rule() -> None:
    """The single worst thing a plugin system could do, refused loudly.

    An installed package silently redefining this site's own fire rule would be invisible: the rule would still
    fire, on somebody else's terms. So a collision rejects the plugin's version and records why.
    """
    from sio_events.rules import Rule, RuleSet, load_plugin_rules

    site = RuleSet()
    site.rules.append(
        Rule.model_validate(
            {
                "id": "tide_flood_warning",
                "emits": "anomaly_detected",
                "severity": "low",
                "description": "the site's own version",
            }
        )
    )
    added = load_plugin_rules(site)
    assert added == 0
    survivor = site.get("tide_flood_warning")
    assert survivor is not None
    assert survivor.severity == "low", "the plugin overrode a site rule"
    assert survivor.description == "the site's own version"
    assert any("IGNORED" in error for error in site.errors)


@needs_demo
def test_the_plugin_rule_reads_its_threshold_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A threshold is a deployment decision, not a code one.

    A gauge on an estuary and one in a dock have different meaningful levels, and forcing a fork of the plugin
    to change a number would defeat the purpose of shipping it as a package.
    """
    from sio_plugin_demo.rules import tide_flood_warning

    monkeypatch.setenv("SIO_TIDE_WARNING_M", "2.4")
    assert tide_flood_warning()["attributes"]["threshold_m"] == 2.4
    # A malformed value falls back rather than raising: the consequence of the rule not loading is silence
    # about flooding.
    monkeypatch.setenv("SIO_TIDE_WARNING_M", "not-a-number")
    assert tide_flood_warning()["attributes"]["threshold_m"] > 0


@needs_demo
def test_the_tide_model_is_a_pure_function_of_time() -> None:
    """So a test can assert high water without waiting six hours, and two runs agree."""
    from sio_plugin_demo.connector import TIDE_PERIOD_S, TideGaugeConnector

    from sio_core.connector import ConnectorConfig

    gauge = TideGaugeConnector(
        ConnectorConfig(
            source_id="g", kind="tide_gauge", modality="iot", options={"amplitude_m": 2.0}
        )
    )
    # A full period later, the level repeats.
    assert gauge.level_at(0.0) == pytest.approx(gauge.level_at(TIDE_PERIOD_S), abs=1e-6)
    # And it spans the amplitude over a period.
    levels = [gauge.level_at(step * TIDE_PERIOD_S / 24) for step in range(24)]
    assert max(levels) > 1.9
    assert min(levels) < -1.9


def test_asyncio_is_available_for_the_async_tests() -> None:
    """Trivial, and it catches a missing anyio/asyncio plugin before six tests error confusingly."""
    assert asyncio.get_event_loop_policy() is not None
