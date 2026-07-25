"""Out-of-tree extension, through entry points (PRD M22, Phase 6).

The claim this module has to make true is specific: **a new connector and a new rule can be added without core
changes.** Not "with a small patch", not "by dropping a file in the right directory" — by installing a package.

That claim was already half-built and, in the way this codebase keeps rediscovering, **nothing called it**. The
ingest service discovered connector plugins into a registry and then constructed its connectors from a
hard-coded list, so a plugin was registered and never instantiated. Discovery without instantiation is
decoration, and it looks exactly like a working plugin system right up to the point somebody writes a plugin.

Four groups, because four things are worth extending:

| group | what it adds | consumed by |
|---|---|---|
| `sio.connectors` | a data source | ingest |
| `sio.rules` | an event rule | events |
| `sio.tools` | a copilot tool | copilot |
| `sio.agents` | an autonomous agent | agents |

**Failures are logged and skipped, never fatal.** A third-party plugin must not be able to stop the platform
from starting — but the operator has to be told *which* plugin failed and why, because a plugin that silently
does not load is indistinguishable from one that loaded and does nothing.

**Nothing is enabled merely by being installed.** A connector plugin declares what it *can* do; a
configuration file says what should actually run, with what options. Installing a package must not start
reading somebody's camera.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from .telemetry import describe_error, get_logger

log = get_logger("sio.plugins")

#: The entry-point groups the platform reads.
GROUPS: tuple[str, ...] = ("sio.connectors", "sio.rules", "sio.tools", "sio.agents")

#: Where a deployment says which plugin connectors to run, and with what options.
#:
#: A file rather than an environment variable, because a connector needs structured options — a URL, a
#: latitude, a poll interval — and packing those into an env var produces the kind of string nobody can review.
DEFAULT_CONFIG = Path(".sio/plugins.json")


@dataclass
class Discovered:
    """What was found, what failed, and why.

    Failures are part of the result rather than only a log line. `/connectors` and `/health` render this, so an
    operator can see that a plugin was installed and rejected without going to the logs — which is the
    difference between a plugin system somebody can debug and one they cannot.
    """

    group: str
    loaded: dict[str, Any] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "loaded": sorted(self.loaded),
            "failed": self.failed,
        }


def discover(group: str, *, expect: type | None = None) -> Discovered:
    """Load every entry point in a group.

    `expect` is checked when given, and a mismatch is a *reported failure* rather than a silent skip. A plugin
    author who exports the wrong object gets told; the alternative is a plugin that installs cleanly, appears
    nowhere, and produces no explanation.
    """
    result = Discovered(group=group)
    for entry in entry_points(group=group):
        try:
            candidate = entry.load()
        except Exception as exc:
            result.failed[entry.name] = describe_error(exc)
            log.error(
                "plugins.load_failed", group=group, name=entry.name, error=describe_error(exc)
            )
            continue

        if expect is not None and not _is_acceptable(candidate, expect):
            reason = (
                f"expected a {expect.__name__} subclass or instance, got {type(candidate).__name__}"
            )
            result.failed[entry.name] = reason
            log.warning("plugins.rejected", group=group, name=entry.name, reason=reason)
            continue

        result.loaded[entry.name] = candidate
        log.info("plugins.loaded", group=group, name=entry.name)
    return result


def _is_acceptable(candidate: Any, expect: type) -> bool:
    """Accept a subclass or an instance.

    Both, because the natural export differs by group: a connector is naturally a class (ingest constructs one
    per configured source), while a rule is naturally a value (there is nothing to construct). Forcing one
    shape on both would make every plugin author in one group write a pointless wrapper.
    """
    if isinstance(candidate, type):
        return issubclass(candidate, expect)
    return isinstance(candidate, expect)


def discover_all() -> dict[str, Discovered]:
    """Every group, for the `/plugins` endpoint and for `just doctor`."""
    return {group: discover(group) for group in GROUPS}


# --------------------------------------------------------------------------- configuration
@dataclass
class PluginConnectorConfig:
    """One connector a deployment has chosen to run."""

    source_id: str
    kind: str
    enabled: bool = True
    label: str | None = None
    modality: str = "manual"
    rate_hz: float = 1.0
    options: dict[str, Any] = field(default_factory=dict)


def load_plugin_config(path: Path | None = None) -> list[PluginConnectorConfig]:
    """Read the connector configuration, or return nothing.

    A missing file is the normal case and not a warning: most deployments run no plugin connectors, and warning
    about the absence of an optional file trains operators to ignore warnings.

    A malformed file *is* a warning — loudly — because somebody wrote it intending it to work, and silently
    ignoring their configuration is the worst of the three possible behaviours. It is not fatal: refusing to
    start the whole ingest service because one plugin stanza has a typo would take the platform down for a
    non-essential extension.
    """
    path = path or DEFAULT_CONFIG
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.error(
            "plugins.config_unreadable",
            path=str(path),
            error=describe_error(exc),
            consequence="no plugin connectors will run",
        )
        return []

    entries = raw.get("connectors", raw) if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        log.error(
            "plugins.config_invalid",
            path=str(path),
            reason="expected a list of connectors, or an object with a 'connectors' key",
        )
        return []

    configs: list[PluginConnectorConfig] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            log.warning("plugins.config_entry_invalid", index=index, reason="not an object")
            continue
        missing = [key for key in ("source_id", "kind") if not entry.get(key)]
        if missing:
            log.warning(
                "plugins.config_entry_incomplete",
                index=index,
                missing=missing,
                reason="a connector needs a source_id and a kind",
            )
            continue
        configs.append(
            PluginConnectorConfig(
                source_id=str(entry["source_id"]),
                kind=str(entry["kind"]),
                enabled=bool(entry.get("enabled", True)),
                label=entry.get("label"),
                modality=str(entry.get("modality", "manual")),
                rate_hz=float(entry.get("rate_hz", 1.0)),
                options=dict(entry.get("options") or {}),
            )
        )
    if configs:
        log.info(
            "plugins.config_loaded",
            path=str(path),
            connectors=[config.source_id for config in configs if config.enabled],
        )
    return configs


__all__ = [
    "DEFAULT_CONFIG",
    "GROUPS",
    "Discovered",
    "PluginConnectorConfig",
    "discover",
    "discover_all",
    "load_plugin_config",
]
