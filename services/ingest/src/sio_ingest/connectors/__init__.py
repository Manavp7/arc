"""Connectors: the pluggable edge of the platform (PRD M1)."""

from .base import (
    Connector,
    ConnectorConfig,
    build_connector,
    build_connectors,
    connector_kinds,
    discover_plugins,
    register_connector,
)
from .simulator import SimulatorConnector
from .weather import OpenMeteoConnector

__all__ = [
    "Connector",
    "ConnectorConfig",
    "OpenMeteoConnector",
    "SimulatorConnector",
    "build_connector",
    "build_connectors",
    "connector_kinds",
    "discover_plugins",
    "register_connector",
]
