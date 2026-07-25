"""Connectors: the pluggable edge of the platform (PRD M1).

Every Phase 7 connector is imported here so its `@register_connector` decorator runs — a connector that is never
imported is never registered, and `build_connector` would report it as unknown while the file sat there looking
complete.

The Phase 7 connectors carry **optional dependencies**, and the import must therefore survive their absence: the
module-level import of each connector file must not itself import `cv2`, `paho`, `pymavlink` or `sqlalchemy`.
Each does that lookup inside `start()` and raises a message naming the extra to install. A default `uv sync` that
pulled in OpenCV, a MAVLink dialect generator and a database driver would take minutes and serve almost nobody.
"""

from .base import (
    Connector,
    ConnectorConfig,
    build_connector,
    build_connectors,
    connector_kinds,
    discover_plugins,
    register_connector,
)
from .drone import MavlinkDroneConnector
from .enterprise import CsvEnterpriseConnector, SqlEnterpriseConnector
from .mqtt import MqttConnector
from .rtsp import RtspCameraConnector
from .satellite import StacSatelliteConnector
from .simulator import SimulatorConnector
from .traffic import TrafficConnector
from .weather import OpenMeteoConnector

__all__ = [
    "Connector",
    "ConnectorConfig",
    "CsvEnterpriseConnector",
    "MavlinkDroneConnector",
    "MqttConnector",
    "OpenMeteoConnector",
    "RtspCameraConnector",
    "SimulatorConnector",
    "SqlEnterpriseConnector",
    "StacSatelliteConnector",
    "TrafficConnector",
    "build_connector",
    "build_connectors",
    "connector_kinds",
    "discover_plugins",
    "register_connector",
]
