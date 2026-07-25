"""STAC satellite imagery — Sentinel-2 via Earth Search (PRD M1, Phase 7).

Satellite is the one modality on a completely different timescale from everything else here. A camera gives 15
frames a second; Sentinel-2 revisits a point every five days and half those passes are cloudy. A connector that
polled it like a camera would hammer a public API for nothing, so this one is built around **change** rather than
around a rate: it asks "is there a scene I have not seen?" and almost always the answer is no.

That shapes three decisions.

**Cloud cover is a filter, not metadata.** A 90%-cloud scene is not a worse observation, it is not an observation
— the site is not in it. Ingesting it would put a white square into the world model and a spurious "conditions
changed" into the event stream. The default ceiling is 30%.

**Assets are fetched to object storage, not held in memory.** A Sentinel-2 band is 100MB+. The observation
carries a reference; the bytes live in MinIO, which is where the perception service already looks for frames.

**No API key.** Earth Search (`earth-search.aws.element84.com`) is open, and Sentinel-2 L2A on AWS is free. A
connector that needs a credential is one nobody tries, and being able to demonstrate real satellite ingest with
no signup is worth more than the extra collections a keyed provider would offer.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from sio_schemas import Geo, Modality, Observation, utc_now

from .base import Connector, ConnectorConfig, register_connector

#: Earth Search v1, the open STAC API in front of Sentinel-2 on AWS.
DEFAULT_STAC_URL = "https://earth-search.aws.element84.com/v1/search"
DEFAULT_COLLECTION = "sentinel-2-l2a"

#: Bands worth fetching by default.
#:
#: True colour and near-infrared. NIR is included because vegetation and water separate from bare ground in it,
#: which is what makes a satellite pass useful for a yard rather than merely pretty.
DEFAULT_ASSETS = ("visual", "nir")

#: The default cloud ceiling, as a percentage.
DEFAULT_MAX_CLOUD = 30.0


@register_connector
class StacSatelliteConnector(Connector):
    """Polls a STAC API for new scenes over the site, and fetches their assets.

    Deliberately tolerant of an empty result: on most days there is no new scene, and a connector that logged a
    warning each time would train its operator to ignore the log.
    """

    kind = "satellite_stac"
    modality = Modality.SATELLITE

    def __init__(self, config: ConnectorConfig) -> None:
        super().__init__(config)
        options = config.options
        self.stac_url = str(options.get("stac_url", DEFAULT_STAC_URL))
        self.collection = str(options.get("collection", DEFAULT_COLLECTION))
        self.lat = float(options.get("lat", 37.7749))
        self.lon = float(options.get("lon", -122.4194))
        # A bounding box around the point, in degrees. Small: a yard is a few hundred metres, and asking for a
        # degree of latitude would return scenes covering half a state.
        self.box_deg = float(options.get("box_deg", 0.05))
        self.max_cloud = float(options.get("max_cloud_percent", DEFAULT_MAX_CLOUD))
        self.lookback_days = int(options.get("lookback_days", 14))
        self.assets = tuple(options.get("assets") or DEFAULT_ASSETS)
        # Six hours. Sentinel-2's revisit is ~5 days, so anything faster is asking a public API to tell us
        # nothing has changed, repeatedly.
        self.interval_s = float(options.get("interval_s", 21600))
        self.fetch_assets = bool(options.get("fetch_assets", True))
        self.once = bool(options.get("once", False))
        self._client: Any = None
        self._store: Any = None
        self._seen: set[str] = set()
        self._scenes = 0
        self._skipped_cloud = 0
        self._error: str | None = None

    async def start(self) -> None:
        import httpx

        # A long timeout: a STAC search across a fortnight of a global collection is not fast, and a 15s
        # timeout produces a connector that appears broken on a slow day.
        self._client = httpx.AsyncClient(timeout=60.0)
        if self.fetch_assets:
            try:
                from sio_core import get_blob

                self._store = get_blob()
            except Exception as error:
                # Degraded, not fatal. Scene METADATA is useful on its own — a cloud-free pass at 11:04 is a
                # fact worth recording even if the bytes cannot be stored — so losing object storage should
                # not take the connector down with it.
                self.log.warning("satellite.no_blob_store", error=str(error))
                self._store = None

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

    @property
    def bbox(self) -> list[float]:
        half = self.box_deg / 2
        return [self.lon - half, self.lat - half, self.lon + half, self.lat + half]

    async def _poll(self) -> list[Observation]:
        since = datetime.now(UTC) - timedelta(days=self.lookback_days)
        body = {
            "collections": [self.collection],
            "bbox": self.bbox,
            "datetime": f"{since.isoformat().replace('+00:00', 'Z')}/..",
            # Sorted newest first and capped, because the interesting scene is the latest one and a fortnight of
            # a global collection is a lot of JSON to parse for a field we only read from the top.
            "sortby": [{"field": "properties.datetime", "direction": "desc"}],
            "limit": 10,
            "query": {"eo:cloud_cover": {"lt": self.max_cloud}},
        }

        try:
            response = await self._client.post(self.stac_url, json=body)  # type: ignore[union-attr]
            if response.status_code >= 400:
                self._error = f"STAC search returned {response.status_code}: {response.text[:160]}"
                self.log.warning("satellite.search_failed", detail=self._error)
                return []
            payload = response.json()
        except Exception as error:
            self._error = f"{type(error).__name__}: {error}"
            self.log.warning("satellite.unreachable", error=self._error)
            return []

        self._error = None
        features = payload.get("features") or []
        observations: list[Observation] = []
        for feature in features:
            scene_id = str(feature.get("id") or "")
            if not scene_id or scene_id in self._seen:
                continue
            properties = feature.get("properties") or {}
            cloud = _number(properties.get("eo:cloud_cover"))
            if cloud is not None and cloud > self.max_cloud:
                # Belt and braces: the server-side `query` should have excluded it, but not every STAC
                # implementation honours `query`, and a 90%-cloud scene reaching the world model would put a
                # white square in it.
                self._skipped_cloud += 1
                continue

            self._seen.add(scene_id)
            self._scenes += 1
            stored = await self._store_assets(scene_id, feature)
            observations.append(
                Observation(
                    source_id=self.source_id,
                    modality=self.modality,
                    ts=utc_now(),
                    geo=Geo(lat=self.lat, lon=self.lon),
                    # The visual band as `raw_ref`, so anything that consumes imagery reaches a satellite tile
                    # the same way it reaches a camera frame. The rest of the bands stay in the payload.
                    raw_ref=stored.get("visual") or next(iter(stored.values()), None),
                    payload={
                        "scene_id": scene_id,
                        "collection": self.collection,
                        # The scene's OWN timestamp, distinct from `ts` above. A pass from three days ago
                        # ingested today is an observation of three days ago, and conflating the two would let
                        # a satellite scene contradict a camera about the present.
                        "captured_ts": properties.get("datetime"),
                        "cloud_percent": cloud,
                        "platform": properties.get("platform"),
                        "bbox": feature.get("bbox"),
                        "assets": stored,
                        "asset_urls": {
                            name: (feature.get("assets") or {}).get(name, {}).get("href")
                            for name in self.assets
                        },
                        "label": f"{self.collection} {scene_id}",
                    },
                )
            )
        if features and not observations:
            self.log.debug("satellite.nothing_new", candidates=len(features))
        return observations

    async def _store_assets(self, scene_id: str, feature: dict[str, Any]) -> dict[str, str]:
        """Fetch each requested band into object storage, returning the keys.

        Failures are per-asset and non-fatal: a scene whose NIR band 404s is still a scene, and its visual band
        is still worth having. Returning what succeeded beats returning nothing.
        """
        if not self.fetch_assets or self._store is None:
            return {}
        stored: dict[str, str] = {}
        assets = feature.get("assets") or {}
        for name in self.assets:
            href = (assets.get(name) or {}).get("href")
            if not href:
                continue
            try:
                response = await self._client.get(href, follow_redirects=True)  # type: ignore[union-attr]
                if response.status_code >= 400:
                    self.log.warning(
                        "satellite.asset_failed", asset=name, status=response.status_code
                    )
                    continue
                key = f"satellite/{self.collection}/{scene_id}/{name}"
                await self._store.put(key, response.content, content_type="image/tiff")
                stored[name] = key
            except Exception as error:
                self.log.warning("satellite.asset_error", asset=name, error=str(error))
        return stored

    async def health(self) -> str:
        if self._error:
            return f"degraded: {self._error}"
        suffix = f", {self._skipped_cloud} too cloudy" if self._skipped_cloud else ""
        return f"ok ({self._scenes} scenes{suffix})"

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "collection": self.collection,
            "bbox": self.bbox,
            "max_cloud_percent": self.max_cloud,
            "scenes": self._scenes,
            "skipped_cloud": self._skipped_cloud,
        }


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "DEFAULT_ASSETS",
    "DEFAULT_COLLECTION",
    "DEFAULT_MAX_CLOUD",
    "DEFAULT_STAC_URL",
    "StacSatelliteConnector",
]
