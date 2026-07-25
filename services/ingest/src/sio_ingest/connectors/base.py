"""The connector interface (PRD M1) and its registry.

A connector's only job is to turn *something external* into `Observation` envelopes on the bus.
Everything downstream — perception, fusion, the world model — is written against the envelope, not
against the source, which is what lets a new signal type be added without touching the core (PRD
M22 acceptance criterion: "a new connector and a new rule can be added without core changes").

Registration works two ways:

1. **in-tree**: decorate with `@register_connector`;
2. **out-of-tree**: publish a package exposing a `sio.connectors` entry point. `discover_plugins()`
   loads those, so a third party ships a connector as a pip-installable package.
"""

from __future__ import annotations

from collections.abc import Iterable

from sio_core import describe_error, get_logger
from sio_core.connector import Connector, ConnectorConfig

log = get_logger("sio.ingest.connectors")


# The contract itself now lives in `sio_core.connector`, and is re-exported here.
#
# It moved because an out-of-tree connector had to import it, and importing it from a SERVICE means a plugin is
# coupled to that service's private module — which makes "a new connector can be added without core changes"
# nearly true rather than true. "We did not change the core, only the module your plugin imported" is not a
# distinction anybody accepts.
#
# Re-exported rather than relocated-and-updated, so every in-tree connector's import is unchanged. The REGISTRY
# stays here: what a connector IS is a platform contract, while WHICH connectors are running is this service's
# concern.

_REGISTRY: dict[str, type[Connector]] = {}


def register_connector(cls: type[Connector]) -> type[Connector]:
    """Register a connector class under its ``kind``."""
    if cls.kind in _REGISTRY and _REGISTRY[cls.kind] is not cls:
        raise ValueError(
            f"connector kind {cls.kind!r} is already registered by {_REGISTRY[cls.kind]}"
        )
    _REGISTRY[cls.kind] = cls
    return cls


def connector_kinds() -> list[str]:
    return sorted(_REGISTRY)


def build_connector(config: ConnectorConfig) -> Connector:
    cls = _REGISTRY.get(config.kind)
    if cls is None:
        raise KeyError(
            f"unknown connector kind {config.kind!r}; registered: {', '.join(connector_kinds()) or 'none'}"
        )
    return cls(config)


def build_connectors(configs: Iterable[ConnectorConfig]) -> list[Connector]:
    return [build_connector(config) for config in configs if config.enabled]


def discover_plugins(group: str = "sio.connectors") -> int:
    """Load out-of-tree connectors advertised through entry points.

    Failures are logged and skipped rather than fatal: a broken third-party plugin must not stop
    the platform from starting, and the operator needs to see *which* plugin failed.
    """
    from importlib.metadata import entry_points

    loaded = 0
    for entry in entry_points(group=group):
        try:
            candidate = entry.load()
            if isinstance(candidate, type) and issubclass(candidate, Connector):
                register_connector(candidate)
                loaded += 1
                log.info("connector.plugin_loaded", name=entry.name, kind=candidate.kind)
            else:
                log.warning(
                    "connector.plugin_invalid", name=entry.name, reason="not a Connector subclass"
                )
        except Exception as exc:
            log.error("connector.plugin_failed", name=entry.name, error=describe_error(exc))
    return loaded
