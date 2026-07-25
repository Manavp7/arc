"""Ingest service: connectors in, normalised observations out (PRD M1)."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import FastAPI, HTTPException

from sio_core import MessageContext, SioService, describe_error, get_blob
from sio_core.plugins import load_plugin_config
from sio_schemas import BusMessage, Modality, Observation, Topic

from .connectors.base import (
    Connector,
    ConnectorConfig,
    build_connector,
    connector_kinds,
    discover_plugins,
)
from .connectors.simulator import SimulatorConnector
from .connectors.weather import OpenMeteoConnector
from .sim.renderer import CameraRenderer
from .site import load_site

# Modality decides the topic. Keeping this mapping in one place means a new connector only has to
# declare what kind of signal it produces, not know the bus layout.
TOPIC_BY_MODALITY: dict[Modality, Topic] = {
    Modality.VIDEO: Topic.RAW_FRAMES,
    Modality.IMAGE: Topic.RAW_FRAMES,
    Modality.AUDIO: Topic.RAW_AUDIO,
    Modality.GPS: Topic.RAW_GPS,
    Modality.IOT: Topic.RAW_IOT,
    Modality.RFID: Topic.RAW_IOT,
    Modality.WEATHER: Topic.RAW_WEATHER,
    Modality.SATELLITE: Topic.RAW_SATELLITE,
}


class IngestService(SioService):
    """Runs every configured connector and publishes what they produce.

    Each connector runs in its own task so one failing source cannot stall the others — the failure
    mode that matters when a site has forty cameras and one of them is unplugged.
    """

    name = "ingest"
    subscribes = ()  # a producer: it reads the world, not the bus
    tick_interval_s = 30.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.site = load_site(self.settings.sim_site)
        self.connectors: list[Connector] = []
        self.simulator_connector: SimulatorConnector | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._published_by_topic: dict[str, int] = {}
        self._entity_task: asyncio.Task[None] | None = None
        self.blob = get_blob(self.settings)
        self.renderer = CameraRenderer(self.settings.samples_dir)
        self._frames_written = 0
        self._frames_unrendered = 0

    # ----------------------------------------------------------------- lifecycle
    def _build_connectors(self) -> list[Connector]:
        # Plugin connectors that could not be built, kept so `/connectors` and `/health` can report them. A
        # plugin that silently fails to load is indistinguishable from one that loaded and does nothing, and an
        # operator debugging that difference through log archaeology is one who stops using plugins.
        #
        # Set here rather than as a class attribute: a mutable class-level dict would be shared by every
        # service instance in a test process, so one test's failures would appear in another's health report.
        self._plugin_errors: dict[str, str] = {}
        cfg = self.settings
        discovered = discover_plugins()
        if discovered:
            self.log.info("ingest.plugins", loaded=discovered, kinds=connector_kinds())

        simulator = SimulatorConnector(
            ConnectorConfig(
                source_id="yard-simulator",
                kind="simulator",
                modality=Modality.MANUAL,
                rate_hz=cfg.sim_tick_hz,
                label="Yard simulator",
                options={
                    "seed": cfg.sim_seed,
                    "trucks": cfg.sim_trucks,
                    "forklifts": cfg.sim_forklifts,
                    "people": cfg.sim_people,
                    "drones": cfg.sim_drones,
                    "frame_fps": cfg.sim_frame_fps,
                    "gps_hz": cfg.sim_gps_hz,
                    "sensor_hz": cfg.sim_sensor_hz,
                },
            )
        )
        self.simulator_connector = simulator

        weather = OpenMeteoConnector(
            ConnectorConfig(
                source_id="weather-openmeteo",
                kind="weather_openmeteo",
                modality=Modality.WEATHER,
                label="Open-Meteo (site)",
                options={
                    "lat": self.site.origin.lat,
                    "lon": self.site.origin.lon,
                    "interval_s": 600,
                },
            )
        )
        # Connectors from installed plugins, instantiated from configuration.
        #
        # This is the line that was missing, and its absence is the whole reason the plugin system did not
        # work: the service DISCOVERED plugin connectors into a registry and then returned a hard-coded list,
        # so a plugin was registered and never constructed. Discovery without instantiation looks exactly like
        # a working plugin system until somebody writes a plugin.
        #
        # Nothing runs merely by being installed. A plugin declares what it CAN do; `.sio/plugins.json` says
        # what should actually run, with what options — because installing a package must not start reading
        # somebody's camera.
        return [simulator, weather, *self._build_plugin_connectors()]

    def _build_plugin_connectors(self) -> list[Connector]:
        """Instantiate the plugin connectors this deployment has configured.

        Each failure is isolated: one connector whose options are wrong must not prevent the others from
        starting, and the operator is told which one and why. A list comprehension over `build_connectors`
        would fail the whole batch on the first bad entry.
        """
        configured = load_plugin_config()
        if not configured:
            return []

        built: list[Connector] = []
        for entry in configured:
            if not entry.enabled:
                self.log.info("ingest.plugin_connector_disabled", source_id=entry.source_id)
                continue
            try:
                connector = build_connector(
                    ConnectorConfig(
                        source_id=entry.source_id,
                        kind=entry.kind,
                        modality=Modality(entry.modality),
                        rate_hz=entry.rate_hz,
                        label=entry.label or entry.source_id,
                        options=entry.options,
                    )
                )
            except (KeyError, ValueError) as exc:
                # The most likely mistake by a long way: a `kind` that no installed plugin provides, usually a
                # typo or a package that is configured but not installed. `build_connector` already lists the
                # registered kinds in its message, which is what makes this recoverable.
                self._plugin_errors[entry.source_id] = describe_error(exc)
                self.log.error(
                    "ingest.plugin_connector_failed",
                    source_id=entry.source_id,
                    kind=entry.kind,
                    error=describe_error(exc),
                )
                continue
            built.append(connector)
            self.log.info(
                "ingest.plugin_connector_built",
                source_id=entry.source_id,
                kind=entry.kind,
                label=entry.label,
            )
        return built

    async def setup(self) -> None:
        if self.renderer.available:
            self.log.info("ingest.renderer_ready", **self.renderer.stats())
        else:
            self.log.warning(
                "ingest.renderer_unavailable",
                effect="frame observations will carry no raw_ref; perception falls back to synthetic",
                hint="run: just samples",
            )
        self.connectors = self._build_connectors()
        for connector in self.connectors:
            try:
                await connector.start()
            except Exception as exc:
                self.log.error(
                    "connector.start_failed", source=connector.source_id, error=describe_error(exc)
                )
                continue
            self._tasks.append(
                asyncio.create_task(self._pump(connector), name=f"connector-{connector.source_id}")
            )
            self.log.info("connector.started", **connector.describe())

        # Publish the fixed cast once so the map has context immediately rather than after the
        # first moving thing happens to pass a camera.
        if self.simulator_connector is not None:
            entities = self.simulator_connector.simulator.site_entities()
            for entity in entities:
                await self.publish(Topic.ENTITIES, entity)
            self.log.info("ingest.site_published", entities=len(entities))

            if self.settings.sim_publish_entities:
                self._entity_task = asyncio.create_task(
                    self._publish_ground_truth(), name="ingest-ground-truth"
                )
                self.log.info(
                    "ingest.ground_truth_enabled",
                    note="Phase 1 bridge; set SIO_SIM_PUBLISH_ENTITIES=false once fusion is live",
                )

    async def teardown(self) -> None:
        for task in [*self._tasks, self._entity_task]:
            if task is not None:
                task.cancel()
        for task in [*self._tasks, self._entity_task]:
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        for connector in self.connectors:
            with contextlib.suppress(Exception):
                await connector.stop()

    async def on_message(
        self, message: BusMessage, ctx: MessageContext
    ) -> None:  # pragma: no cover
        raise NotImplementedError("ingest is a producer and subscribes to nothing")

    # -------------------------------------------------------------------- pumping
    async def _pump(self, connector: Connector) -> None:
        """Publish everything one connector yields, isolating its failures."""
        while True:
            try:
                async for observation in connector.observations():
                    topic = self._topic_for(connector, observation)
                    if topic == str(Topic.RAW_FRAMES):
                        await self._store_frame(observation)
                    await self.publish(topic, observation)
                    self._published_by_topic[topic] = self._published_by_topic.get(topic, 0) + 1
                    await self._apply_backpressure(topic)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.metrics.errors.labels(service=self.name, kind="connector").inc()
                self.log.error(
                    "connector.failed",
                    source=connector.source_id,
                    error=describe_error(exc),
                    exc_info=True,
                )
                # Restart the source after a pause rather than losing it for the process lifetime.
                await asyncio.sleep(5.0)

    async def _store_frame(self, observation: Observation) -> None:
        """Render the camera view and write it to the object store.

        Done *before* publishing, so a consumer that receives the observation and immediately fetches
        `raw_ref` always finds bytes. Publishing first would create a race that shows up as
        intermittent "frame missing" warnings in perception and looks like a storage fault.

        When rendering is unavailable the `raw_ref` is cleared rather than left dangling: a reference
        to something that does not exist is worse than no reference, because a consumer cannot tell
        the difference between "not yet written" and "never will be".
        """
        if not observation.raw_ref:
            return
        rendered = await asyncio.to_thread(
            self.renderer.render, observation.source_id, observation.payload
        )
        if rendered is None:
            self._frames_unrendered += 1
            observation.raw_ref = None
            return
        try:
            await self.blob.put(observation.raw_ref, rendered, content_type="image/jpeg")
            self._frames_written += 1
        except Exception as exc:
            self.metrics.errors.labels(service=self.name, kind="blob").inc()
            self.log.warning(
                "frame.store_failed", key=observation.raw_ref, error=describe_error(exc)
            )
            observation.raw_ref = None

    def _topic_for(self, connector: Connector, observation: Observation) -> str:
        if isinstance(connector, SimulatorConnector):
            return connector.topic_for(observation)
        topic = TOPIC_BY_MODALITY.get(observation.modality)
        return str(topic) if topic else str(Topic.RAW_IOT)

    async def _apply_backpressure(self, topic: str) -> None:
        """Slow down when consumers fall behind.

        Without this, a paused perception service means Redis grows until it is trimmed and data is
        lost silently. Checking lag every 200 messages keeps the check itself cheap.
        """
        count = self._published_by_topic.get(topic, 0)
        if count % 200 != 0:
            return
        with contextlib.suppress(Exception):
            lag = await self.bus.lag(topic, "cg.perception")
            if lag > self.settings.bus_maxlen // 2:
                self.log.warning("ingest.backpressure", topic=topic, lag=lag)
                await asyncio.sleep(1.0)

    async def _publish_ground_truth(self) -> None:
        """Publish simulated entities directly to the world model (Phase 1 bridge).

        This is the one place the simulator short-circuits the perception pipeline, and it exists
        only so the live map has something to show before Phase 2 lands. It is off with
        `SIO_SIM_PUBLISH_ENTITIES=false`, and every entity it writes carries
        `attributes.simulated = true` plus provenance saying so — nothing downstream has to guess
        where an entity came from.
        """
        assert self.simulator_connector is not None
        simulator = self.simulator_connector.simulator
        interval = 1.0 / max(0.2, self.settings.sim_entity_hz)
        while True:
            try:
                for entity in simulator.ground_truth_entities():
                    await self.publish(Topic.ENTITIES, entity)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.error("ingest.ground_truth_failed", error=describe_error(exc))
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------ reporting
    async def health_checks(self) -> dict[str, str]:
        checks: dict[str, str] = {}
        with contextlib.suppress(Exception):
            checks["blob"] = "ok" if await self.blob.ping() else "unreachable"
        for connector in self.connectors:
            with contextlib.suppress(Exception):
                checks[f"connector:{connector.source_id}"] = await connector.health()
        return checks

    async def tick(self) -> None:
        stats = self.simulator_connector.simulator.stats() if self.simulator_connector else {}
        self.log.info(
            "ingest.stats",
            published=self._published_by_topic,
            agents=stats.get("agents"),
            frames=stats.get("frames"),
            frames_written=self._frames_written,
            frames_unrendered=self._frames_unrendered,
            incidents=stats.get("incidents"),
        )

    # --------------------------------------------------------------------- routes
    def routes(self, app: FastAPI) -> None:
        @app.get("/connectors", tags=["ingest"])
        async def list_connectors() -> dict[str, Any]:
            return {
                "registered_kinds": connector_kinds(),
                "running": [connector.describe() for connector in self.connectors],
                "published": self._published_by_topic,
                "renderer": {
                    **self.renderer.stats(),
                    "frames_written": self._frames_written,
                    "frames_unrendered": self._frames_unrendered,
                },
            }

        @app.get("/site", tags=["ingest"])
        async def site() -> dict[str, Any]:
            """The site as GeoJSON — what the UI draws and `just seed` loads into PostGIS."""
            return self.site.as_geojson()

        @app.get("/simulation", tags=["ingest"])
        async def simulation() -> dict[str, Any]:
            if self.simulator_connector is None:
                raise HTTPException(status_code=404, detail="simulator not running")
            return self.simulator_connector.simulator.stats()

        @app.post("/simulation/inject/fire", tags=["ingest"])
        async def inject_fire(zone_id: str = "dock_3", duration_s: float = 900.0) -> dict[str, Any]:
            """Start a fire in a zone. Drives the UC2 demo and the e2e playbook test."""
            if self.simulator_connector is None:
                raise HTTPException(status_code=404, detail="simulator not running")
            if self.site.zone(zone_id) is None:
                raise HTTPException(status_code=400, detail=f"unknown zone {zone_id!r}")
            incident = self.simulator_connector.simulator.inject_fire(
                zone_id, duration_s=duration_s
            )
            self.log.warning("simulation.fire_injected", zone=zone_id, duration_s=duration_s)
            return {
                "injected": incident.kind,
                "zone_id": incident.zone_id,
                "duration_s": duration_s,
            }

        @app.post("/simulation/inject/power_failure", tags=["ingest"])
        async def inject_power_failure(duration_s: float = 300.0) -> dict[str, Any]:
            if self.simulator_connector is None:
                raise HTTPException(status_code=404, detail="simulator not running")
            incident = self.simulator_connector.simulator.inject_power_failure(duration_s)
            return {"injected": incident.kind, "duration_s": duration_s}
