"""A tide gauge connector, added from outside the tree.

Deliberately something the platform has never seen. A plugin that adds a second camera connector proves very
little — the platform already knows what a camera is, and the interesting question is whether it can accept a
*kind* of signal nobody anticipated. A tide gauge publishes a water level in metres, which no in-tree connector
produces and no in-tree rule looks at.

Note what this file imports: `sio_core` and `sio_schemas`, and nothing else. If it needed `sio_ingest` the claim
would be false — a plugin that reaches into a service's internals for its interface is a fork with extra steps.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncIterator
from typing import Any, ClassVar

from sio_core.connector import Connector, ConnectorConfig
from sio_schemas import Geo, Modality, Observation, utc_now

#: A semidiurnal tide: two highs and two lows a day, which is the pattern on most coasts.
TIDE_PERIOD_S = 12 * 3600 + 25 * 60


class TideGaugeConnector(Connector):
    """Publishes a water level, on an interval, from a simulated gauge.

    Simulated rather than reading a real gauge, because the point of this package is to demonstrate the
    *mechanism* and a real gauge would make the demonstration depend on somebody's API key and network. The
    shape of the code is what a real connector looks like: poll, build an `Observation`, yield it, sleep.
    """

    #: The string a deployment writes in `.sio/plugins.json` to run this. It has to be unique across every
    #: installed plugin, and the registry raises on a collision rather than letting one silently win.
    kind: ClassVar[str] = "tide_gauge"
    modality: ClassVar[Modality] = Modality.IOT

    def __init__(self, config: ConnectorConfig) -> None:
        super().__init__(config)
        options = config.options
        self.lat = float(options.get("lat", 37.7764))
        self.lon = float(options.get("lon", -122.4189))
        self.interval_s = float(options.get("interval_s", 60.0))
        #: Metres between mean level and high water.
        self.amplitude_m = float(options.get("amplitude_m", 1.8))
        self.mean_level_m = float(options.get("mean_level_m", 0.0))
        self.readings = 0

    async def start(self) -> None:
        # Logged at startup so an operator can see the plugin is live and with what settings. A plugin that
        # loads silently is one nobody can tell apart from a plugin that failed to load.
        self.log.info(
            "tide_gauge.started",
            lat=self.lat,
            lon=self.lon,
            interval_s=self.interval_s,
            amplitude_m=self.amplitude_m,
        )

    def level_at(self, when: float) -> float:
        """Water level in metres at a wall-clock time.

        A pure function of time, so a test can assert the tide reaches high water without waiting six hours —
        and so two runs of the connector agree about what the tide was doing.
        """
        phase = (when % TIDE_PERIOD_S) / TIDE_PERIOD_S * 2 * math.pi
        return self.mean_level_m + self.amplitude_m * math.sin(phase)

    async def observations(self) -> AsyncIterator[Observation]:
        while True:
            level = self.level_at(time.time())
            self.readings += 1
            yield Observation(
                tenant_id=self.config.options.get("tenant_id", "default"),
                source_id=self.source_id,
                modality=Modality.IOT,
                ts=utc_now(),
                geo=Geo(lat=self.lat, lon=self.lon),
                confidence=0.95,
                payload={
                    # `water_level_m` is the field the rule in `rules.py` reads. A connector and a rule from the
                    # same plugin agreeing on a field name is the normal way an extension is coherent — and
                    # nothing in the core needs to know the field exists.
                    "water_level_m": round(level, 3),
                    "gauge": self.source_id,
                    "readings": self.readings,
                },
            )
            await asyncio.sleep(self.interval_s)

    async def health(self) -> str:
        if self.readings == 0:
            # Not an error: on a sixty-second interval the first reading legitimately has not happened yet, and
            # reporting "unhealthy" for that would make every restart look like a failure.
            return "ok (no reading yet)"
        return f"ok ({self.readings} reading(s), last {self.level_at(time.time()):+.2f} m)"

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "plugin": "sio-plugin-demo",
            "readings": self.readings,
            "tide_period_hours": round(TIDE_PERIOD_S / 3600, 2),
        }


__all__ = ["TIDE_PERIOD_S", "TideGaugeConnector"]
