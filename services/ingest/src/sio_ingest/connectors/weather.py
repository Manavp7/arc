"""Open-Meteo weather connector — a real external signal, no API key required.

Included in Phase 1 on purpose: with only a simulator, the connector interface is never tested
against the messiness of a real source (network failures, rate limits, a schema someone else
controls). Weather is also genuinely used downstream — wind speed and direction feed the fire-spread
simulation (PRD M11), and temperature contextualises the thermal sensors.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from sio_schemas import Geo, Modality, Observation, utc_now

from .base import Connector, ConnectorConfig, register_connector

ENDPOINT = "https://api.open-meteo.com/v1/forecast"
FIELDS = "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,precipitation,weather_code"


@register_connector
class OpenMeteoConnector(Connector):
    """Polls Open-Meteo for current conditions at the site."""

    kind = "weather_openmeteo"
    modality = Modality.WEATHER

    def __init__(self, config: ConnectorConfig) -> None:
        super().__init__(config)
        options = config.options
        self.lat = float(options.get("lat", 37.7749))
        self.lon = float(options.get("lon", -122.4194))
        # Default 10 minutes: weather does not change faster than that, and a polite polling rate
        # keeps a free public API usable.
        self.interval_s = float(options.get("interval_s", 600))
        self._client: Any = None
        self._failures = 0

    async def start(self) -> None:
        import httpx

        self._client = httpx.AsyncClient(timeout=15.0)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def observations(self) -> AsyncIterator[Observation]:
        while True:
            observation = await self._fetch()
            if observation is not None:
                yield observation
            await asyncio.sleep(self.interval_s)

    async def _fetch(self) -> Observation | None:
        if self._client is None:
            await self.start()
        assert self._client is not None
        params = {
            "latitude": self.lat,
            "longitude": self.lon,
            "current": FIELDS,
            "timezone": "UTC",
        }
        try:
            response = await self._client.get(ENDPOINT, params=params)
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            # A weather outage must not affect the pipeline: log, count, and try again later.
            self._failures += 1
            self.log.warning("weather.fetch_failed", error=str(exc), failures=self._failures)
            return None

        current = body.get("current") or {}
        if not current:
            self.log.warning("weather.empty_response")
            return None
        self._failures = 0

        return Observation(
            source_id=self.source_id,
            modality=Modality.WEATHER,
            ts=(current.get("time") and f"{current['time']}Z") or utc_now(),
            geo=Geo(lat=self.lat, lon=self.lon),
            confidence=0.95,
            payload={
                "temperature_c": current.get("temperature_2m"),
                "humidity_pct": current.get("relative_humidity_2m"),
                "wind_speed_ms": current.get("wind_speed_10m"),
                "wind_direction_deg": current.get("wind_direction_10m"),
                "precipitation_mm": current.get("precipitation"),
                "weather_code": current.get("weather_code"),
                "provider": "open-meteo",
            },
        )

    async def health(self) -> str:
        return "ok" if self._failures == 0 else f"degraded: {self._failures} consecutive failures"
