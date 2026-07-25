"""SIO what-if simulation (PRD M11)."""

from .scenarios import SCENARIOS, Projection, Scenario
from .service import SimulationService
from .world import SimEntity, SimZone, WorldSnapshot, snapshot_from_api

__all__ = [
    "SCENARIOS",
    "Projection",
    "Scenario",
    "SimEntity",
    "SimZone",
    "SimulationService",
    "WorldSnapshot",
    "snapshot_from_api",
]
