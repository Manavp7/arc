"""The connector contract, in the public library (PRD M22, Phase 6).

This lives in `sio_core` rather than in `services/ingest` for one reason: **a plugin must be able to depend only
on the public packages.** The contract was originally defined inside the ingest service, which meant an
out-of-tree connector had to `from sio_ingest.connectors.base import Connector` — reaching into a service's
internals for the interface it is meant to implement.

That does not make the plugin system *not work*; it makes the claim "a new connector can be added without core
changes" nearly true instead of true. A plugin coupled to a service's private module breaks when that service
is refactored, and "we did not change the core, only the module your plugin imported" is not a distinction
anybody accepts.

`sio_ingest.connectors.base` re-exports these names, so every existing in-tree connector is unaffected and no
service changed. The registry stays in ingest, where it belongs: *what a connector is* is a platform contract;
*which connectors are running* is one service's concern.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, ClassVar

from sio_schemas import Modality, Observation

from .telemetry import get_logger


@dataclass
class ConnectorConfig:
    """Everything a connector needs to run.

    Deliberately plain data, so it can come from YAML, JSON or an environment variable without a translation
    layer — which is what makes `.sio/plugins.json` possible at all.
    """

    source_id: str
    kind: str
    modality: Modality
    enabled: bool = True
    rate_hz: float = 1.0
    options: dict[str, Any] = field(default_factory=dict)
    label: str | None = None


class Connector(abc.ABC):
    """Base class for every signal source, in-tree or out.

    Subclasses implement :meth:`observations` as an async generator. The ingest service handles publishing,
    backpressure, error isolation and metrics — a connector author writes only the part specific to their
    source, which is the reason this interface is worth being small.

    `start` and `stop` are concrete and empty rather than abstract: most connectors need no setup, and forcing
    every one to write `async def start(self): pass` is noise that obscures the connectors that do.
    """

    kind: ClassVar[str] = "abstract"
    modality: ClassVar[Modality] = Modality.MANUAL

    def __init__(self, config: ConnectorConfig) -> None:
        self.config = config
        self.source_id = config.source_id
        self.log = get_logger(f"sio.ingest.{config.kind}.{config.source_id}")

    async def start(self) -> None:  # noqa: B027 - an optional hook, not an abstract method
        """Acquire resources (open a stream, authenticate). Raise to abort."""

    async def stop(self) -> None:  # noqa: B027 - an optional hook, not an abstract method
        """Release resources. Must not raise.

        Must not, because it runs during shutdown: an exception here would mask whatever prompted the shutdown
        and leave the remaining connectors unstopped.
        """

    @abc.abstractmethod
    def observations(self) -> AsyncIterator[Observation]:
        """Yield observations until cancelled."""

    async def health(self) -> str:
        """A one-line status. Anything not starting with "ok" degrades the service's health."""
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


__all__ = ["Connector", "ConnectorConfig"]
