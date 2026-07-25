"""Traffic conditions on the roads into the site (PRD M1, Phase 7).

Why a logistics yard cares: a truck's arrival time is decided on the motorway, not at the gate. If the approach is
blocked, every dock slot behind it slips, and the platform can say so twenty minutes before the yard notices.

Uses **Overpass plus a live incident feed**, not a commercial routing API, and that is a deliberate trade. Google
and TomTom give better numbers and require a key, a billing account and a rate budget — which means a connector
nobody can try. This one works with no credential, which is worth more for a platform somebody is evaluating; the
seam is `provider`, so a deployment with a TomTom key adds a subclass rather than rewriting the caller.

The default provider is `open_incidents`, a shape rather than a specific vendor: any endpoint returning GeoJSON
features with a `properties.description` works, which covers most national and municipal open-data feeds.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator
from typing import Any

from sio_schemas import Geo, Modality, Observation, utc_now

from .base import Connector, ConnectorConfig, register_connector

#: How far from the site an incident is worth reporting, in kilometres.
#:
#: 25km is roughly half an hour of motorway. Closer than that and the truck is already committed; much further
#: and every incident in the county arrives as an alert about nothing.
DEFAULT_RADIUS_KM = 25.0

EARTH_RADIUS_KM = 6371.0


@register_connector
class TrafficConnector(Connector):
    """Polls an incident feed and reports what is happening on the approaches.

    Tolerant by design: a public feed that is down, rate-limited or returning a schema somebody changed last week
    must degrade to "no traffic data" rather than taking the ingest service with it. This is the connector most
    likely to be pointed at an endpoint nobody controls.
    """

    kind = "traffic_incidents"
    modality = Modality.TRAFFIC

    def __init__(self, config: ConnectorConfig) -> None:
        super().__init__(config)
        options = config.options
        self.lat = float(options.get("lat", 37.7749))
        self.lon = float(options.get("lon", -122.4194))
        self.radius_km = float(options.get("radius_km", DEFAULT_RADIUS_KM))
        self.provider = str(options.get("provider", "open_incidents"))
        self.url = str(options.get("url", ""))
        # Five minutes. Incidents clear on that timescale, and polling faster is asking a public feed to repeat
        # itself.
        self.interval_s = float(options.get("interval_s", 300))
        self.once = bool(options.get("once", False))
        self._client: Any = None
        self._seen: set[str] = set()
        self._reported = 0
        self._out_of_range = 0
        self._error: str | None = None

    async def start(self) -> None:
        import httpx

        if self.provider != "open_incidents":
            raise ValueError(
                f"unknown traffic provider {self.provider!r}. Only 'open_incidents' ships, because a "
                f"connector needing an API key is one nobody tries. Subclass this and override "
                f"`_fetch` for TomTom, HERE or Google."
            )
        if not self.url:
            raise ValueError(
                f"{self.kind} needs options.url — a GeoJSON incident feed. Most national and municipal "
                f"open-data portals publish one; see docs/CONNECTORS.md."
            )
        self._client = httpx.AsyncClient(timeout=20.0)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def observations(self) -> AsyncIterator[Observation]:
        while True:
            for observation in await self._poll():
                yield observation
            if self.once:
                return
            await asyncio.sleep(self.interval_s)

    async def _poll(self) -> list[Observation]:
        try:
            features = await self._fetch()
        except Exception as error:
            self._error = f"{type(error).__name__}: {error}"
            self.log.warning("traffic.unreachable", error=self._error)
            return []

        self._error = None
        observations: list[Observation] = []
        for feature in features:
            incident = self._to_observation(feature)
            if incident is not None:
                observations.append(incident)
        return observations

    async def _fetch(self) -> list[dict[str, Any]]:
        """The GeoJSON features from the feed.

        Separated from the parsing so a deployment with a commercial key overrides one small method rather than
        reimplementing the distance filter, the deduplication and the observation mapping.
        """
        response = await self._client.get(self.url)  # type: ignore[union-attr]
        if response.status_code >= 400:
            raise RuntimeError(f"feed returned {response.status_code}: {response.text[:120]}")
        body = response.json()
        if isinstance(body, dict):
            return list(body.get("features") or [])
        return list(body) if isinstance(body, list) else []

    def _to_observation(self, feature: dict[str, Any]) -> Observation | None:
        properties = dict(feature.get("properties") or {})
        coordinates = _first_point(feature.get("geometry") or {})
        if coordinates is None:
            return None
        longitude, latitude = coordinates

        distance = _haversine_km(self.lat, self.lon, latitude, longitude)
        if distance > self.radius_km:
            # Filtered here rather than trusting the feed's own bounding box, because most open feeds serve a
            # whole country and every incident in it would otherwise become an observation about our site.
            self._out_of_range += 1
            return None

        identity = str(
            properties.get("id")
            or properties.get("incident_id")
            or f"{latitude:.5f},{longitude:.5f},{properties.get('description', '')[:40]}"
        )
        if identity in self._seen:
            # Incidents persist in a feed for hours. Without deduplication a single closed lane would produce an
            # observation every five minutes for a day, and the event engine would keep re-deciding about it.
            return None
        self._seen.add(identity)
        self._reported += 1

        return Observation(
            source_id=self.source_id,
            modality=self.modality,
            ts=utc_now(),
            geo=Geo(lat=latitude, lon=longitude),
            payload={
                "incident_id": identity,
                "description": properties.get("description") or properties.get("title"),
                "severity": properties.get("severity"),
                "road": properties.get("road") or properties.get("roadName"),
                # The distance is computed here and carried, because "an incident 3km from the gate" and "an
                # incident 24km away" warrant different responses and the consumer should not have to redo the
                # trigonometry to tell them apart.
                "distance_km": round(distance, 2),
                "starts": properties.get("starttime") or properties.get("start"),
                "ends": properties.get("endtime") or properties.get("end"),
                "label": str(properties.get("description") or "traffic incident")[:120],
            },
        )

    async def health(self) -> str:
        if self._error:
            return f"degraded: {self._error}"
        suffix = (
            f", {self._out_of_range} outside {self.radius_km:.0f}km" if self._out_of_range else ""
        )
        return f"ok ({self._reported} incidents{suffix})"

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "provider": self.provider,
            "radius_km": self.radius_km,
            "incidents": self._reported,
        }


def _first_point(geometry: dict[str, Any]) -> tuple[float, float] | None:
    """A representative (lon, lat) for any GeoJSON geometry.

    Incident feeds use Point for a crash, LineString for a closed stretch and occasionally Polygon for a zone.
    Taking the first coordinate of whatever arrives is approximate and correct enough: the question is "is this
    near the site", and the head of a closed stretch answers it.
    """
    coordinates = geometry.get("coordinates")
    if not coordinates:
        return None
    node: Any = coordinates
    # Descend until a pair of numbers.
    for _ in range(4):
        if (
            isinstance(node, list | tuple)
            and len(node) >= 2
            and all(isinstance(item, int | float) for item in node[:2])
        ):
            return float(node[0]), float(node[1])
        if isinstance(node, list | tuple) and node:
            node = node[0]
        else:
            return None
    return None


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres.

    Haversine rather than a flat approximation: an incident feed covers a whole country, and treating degrees as
    a grid is wrong by tens of percent at the latitudes where most freight moves.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


__all__ = ["DEFAULT_RADIUS_KM", "TrafficConnector"]
