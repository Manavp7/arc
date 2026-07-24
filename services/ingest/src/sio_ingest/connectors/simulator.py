"""Simulator connector: the yard as a signal source."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from sio_schemas import Modality, Observation

from ..sim.simulator import YardSimulator
from .base import Connector, ConnectorConfig, register_connector


@register_connector
class SimulatorConnector(Connector):
    """Wraps :class:`YardSimulator` behind the connector interface.

    Exists so the simulator is not a special case: it registers, configures and is monitored
    exactly like an RTSP camera or a weather API. The service can therefore treat "is this a
    simulated site or a real one" as configuration.
    """

    kind = "simulator"
    modality = Modality.MANUAL

    def __init__(self, config: ConnectorConfig, simulator: YardSimulator | None = None) -> None:
        super().__init__(config)
        options = config.options
        self.simulator = simulator or YardSimulator(
            seed=int(options.get("seed", 1337)),
            trucks=int(options.get("trucks", 6)),
            forklifts=int(options.get("forklifts", 3)),
            people=int(options.get("people", 8)),
            drones=int(options.get("drones", 1)),
            frame_fps=float(options.get("frame_fps", 2.0)),
            gps_hz=float(options.get("gps_hz", 1.0)),
            sensor_hz=float(options.get("sensor_hz", 0.2)),
        )
        self.tick_interval_s = 1.0 / max(0.1, config.rate_hz)
        self.topics: dict[str, str] = {}

    async def observations(self) -> AsyncIterator[Observation]:
        """Yield simulated observations forever.

        The topic each observation belongs on is recorded in :attr:`topics` keyed by observation id,
        because the connector interface yields observations while the *routing* is modality-specific
        (a GPS fix belongs on `raw.gps`, a frame on `raw.frames`).
        """
        while True:
            output = self.simulator.step(self.tick_interval_s)
            for topic, observation in output.observations:
                self.topics[observation.id] = topic
                yield observation
            await asyncio.sleep(self.tick_interval_s)

    def topic_for(self, observation: Observation) -> str:
        return self.topics.pop(observation.id, "raw.iot")

    async def health(self) -> str:
        stats = self.simulator.stats()
        return f"ok ({sum(stats['agents'].values())} agents, {stats['frames']} frames)"
