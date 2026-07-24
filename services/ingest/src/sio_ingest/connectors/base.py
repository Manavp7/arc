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

import abc
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from sio_core import get_logger
from sio_schemas import Modality, Observation

log = get_logger("sio.ingest.connectors")


@dataclass
class ConnectorConfig:
    """Everything a connector needs to run. Deliberately plain data, so it can come from YAML."""

    source_id: str
    kind: str
    modality: Modality
    enabled: bool = True
    rate_hz: float = 1.0
    options: dict[str, Any] = field(default_factory=dict)
    label: str | None = None


class Connector(abc.ABC):
    """Base class for every signal source.

    Subclasses implement :meth:`observations` as an async generator. The service handles
    publishing, backpressure, error isolation and metrics — a connector author writes only the
    part that is specific to their source.
    """

    kind: ClassVar[str] = "abstract"
    modality: ClassVar[Modality] = Modality.MANUAL

    def __init__(self, config: ConnectorConfig) -> None:
        self.config = config
        self.source_id = config.source_id
        self.log = get_logger(f"sio.ingest.{config.kind}.{config.source_id}")

    async def start(self) -> None:  # noqa: B027 - an optional hook, not an abstract method
        """Acquire resources (open a stream, authenticate). Raise to abort.

        Deliberately concrete and empty: most connectors need no setup, and forcing every one to
        write ``async def start(self): pass`` would be noise.
        """

    async def stop(self) -> None:  # noqa: B027 - an optional hook, not an abstract method
        """Release resources. Must not raise."""

    @abc.abstractmethod
    def observations(self) -> AsyncIterator[Observation]:
        """Yield observations until cancelled."""

    async def health(self) -> str:
        return "ok"

    def describe(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "kind": self.config.kind,
            "modality": str(self.config.modality),
            "enabled": self.config.enabled,
            "rate_hz": self.config.rate_hz,
            "label": self.config.label,
        }


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
            log.error("connector.plugin_failed", name=entry.name, error=str(exc))
    return loaded
