"""Simulator connector: the yard as a signal source."""

from __future__ import annotations

import asyncio
import time
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
        self.skipped_s = 0.0
        """Simulated time deliberately discarded after a stall, rather than teleporting agents."""

    async def observations(self) -> AsyncIterator[Observation]:
        """Yield simulated observations forever, on a clock that tracks wall time.

        The topic each observation belongs on is recorded in :attr:`topics` keyed by observation id,
        because the connector interface yields observations while the *routing* is modality-specific
        (a GPS fix belongs on ``raw.gps``, a frame on ``raw.frames``).

        **Advance by real elapsed time, and sleep only the remainder of the interval.**

        The first version stepped a fixed ``dt`` and then slept a full ``dt``, so each iteration took
        ``dt + processing time`` of wall clock while advancing the simulation by only ``dt``. Simulated
        time therefore fell behind, permanently and cumulatively: measured on a running stack, every
        payload timestamp was **27 seconds** in the past, which downstream looked like 20-second event
        detection latency and was in fact the clock quietly losing.

        Rendering frames is the expensive part, and it gets slower as more agents come into view, so
        the drift grows with load — the worst shape for a bug, since it is invisible in a short test and
        obvious only after the demo has been running a while.

        Advancing by measured elapsed time means a slow tick moves agents *further* rather than moving
        time backwards. Timestamps then mean what every consumer assumes they mean.
        """
        last = time.monotonic()
        next_tick = last
        while True:
            now = time.monotonic()
            dt = now - last
            last = now
            # Cap a single step. After a long stall — a debugger, a suspended VM, a GC pause of the kind
            # that only happens in front of an audience — a 60-second dt would teleport every agent
            # across the site. Losing the excess is the lesser evil, and it is counted rather than
            # hidden.
            if dt > self.MAX_STEP_S:
                self.skipped_s += dt - self.MAX_STEP_S
                dt = self.MAX_STEP_S

            output = self.simulator.step(dt)
            for topic, observation in output.observations:
                self.topics[observation.id] = topic
                yield observation

            # Absolute schedule, not `sleep(interval)`: sleeping a full interval after doing work makes
            # the tick rate drift by however long the work took, which is the same mistake one level up.
            next_tick += self.tick_interval_s
            await asyncio.sleep(max(0.0, next_tick - time.monotonic()))
            if next_tick < time.monotonic() - self.tick_interval_s:
                # Cannot keep up. Resynchronise rather than accumulating an unbounded backlog of
                # already-late ticks, and let the drift figure in /health say so.
                next_tick = time.monotonic()

    def topic_for(self, observation: Observation) -> str:
        return self.topics.pop(observation.id, "raw.iot")

    MAX_STEP_S = 2.0
    """Largest single simulation step. See the note in `observations`."""

    @property
    def clock_drift_s(self) -> float:
        """How far simulated time has fallen behind wall time.

        Reported because it was invisible for two phases. Near zero is healthy; a growing number means
        the simulation cannot keep up, and every timestamp it emits is that far in the past.
        """
        return self.simulator.wall_elapsed_s - self.simulator.elapsed_s - self.skipped_s

    async def health(self) -> str:
        stats = self.simulator.stats()
        drift = self.clock_drift_s
        detail = (
            f"{sum(stats['agents'].values())} agents, {stats['frames']} frames, drift {drift:+.1f}s"
        )
        # More than a couple of seconds behind and the timestamps are misleading, so say so rather than
        # reporting a cheerful "ok" over a clock that is losing.
        return (
            f"ok ({detail})"
            if abs(drift) < 3.0
            else f"degraded: simulation clock behind ({detail})"
        )
