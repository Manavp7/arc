"""MQTT IoT bridge (PRD M1, Phase 7).

MQTT is how most industrial sensors actually talk, and it inverts this platform's connector model: every other
connector *polls*, and this one is *pushed to*.

**Uses `aiomqtt`, not `paho-mqtt`**, and the reason is worth recording because I wrote the paho version first.
Paho delivers messages on its own network thread, so bridging it to an async generator needs a queue plus
`loop.call_soon_threadsafe` — and then a bounded queue, and then a policy for what to drop when it fills. That is
forty lines of concurrency in a connector, and every line of it is a place to introduce a bug that appears once a
week under load.

`aiomqtt` wraps paho and exposes `async for message in client.messages`, which is exactly the shape
`observations()` needs. The thread bridge disappears, and with it the class of bug where touching an asyncio queue
from another thread corrupts the loop in ways that surface much later as an unrelated hang. It was also already
the declared extra in this service's `pyproject.toml` from an earlier phase — I nearly replaced it with paho
before checking.

Backpressure still needs a decision, and it moves to the client: `max_queued_incoming_messages` bounds what
aiomqtt buffers, and beyond that the broker's own QoS rules apply. For QoS 0 telemetry that is the right
behaviour — the current temperature matters and the one from four seconds ago does not.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from sio_schemas import Geo, Modality, Observation, utc_now

from .base import Connector, ConnectorConfig, register_connector

#: How many messages aiomqtt may buffer before dropping.
#:
#: 1000 is roughly a minute of a busy site at 15Hz — enough to ride out a slow consumer, small enough that a
#: runaway publisher shows up as a bounded buffer rather than as the kernel's OOM killer choosing a victim.
QUEUE_MAX = 1000


@register_connector
class MqttConnector(Connector):
    """Subscribes to MQTT topics and turns messages into observations.

    Optional dependency (`aiomqtt`): a deployment with no broker should not carry a broker client, and the import
    error names the extra to install rather than saying `ModuleNotFoundError`.
    """

    kind = "mqtt_iot"
    modality = Modality.IOT

    def __init__(self, config: ConnectorConfig) -> None:
        super().__init__(config)
        options = config.options
        self.host = str(options.get("host", "127.0.0.1"))
        self.port = int(options.get("port", 1883))
        self.topics = [str(topic) for topic in (options.get("topics") or ["sio/#"])]
        self.username = options.get("username")
        self.password = options.get("password")
        self.qos = int(options.get("qos", 0))
        # Where in the payload to find the useful fields. Nobody's sensor publishes `{"value": ...}` by
        # coincidence, so the mapping is configuration rather than convention.
        self.mapping = dict(options.get("mapping") or {})
        self.client_id = str(options.get("client_id", f"sio-{config.source_id}"))
        self._client: Any = None
        self._received = 0
        self._undecodable = 0
        self._connected = False
        self._error: str | None = None

    async def start(self) -> None:
        try:
            import aiomqtt  # noqa: F401 - imported to fail early with a useful message
        except ImportError as error:
            raise RuntimeError(
                "mqtt_iot needs aiomqtt: `uv pip install 'sio-ingest[mqtt]'`. Optional because a "
                "deployment with no MQTT broker should not carry a broker client."
            ) from error
        # The connection itself is opened inside `observations()`, because aiomqtt's client is an async context
        # manager and its lifetime has to enclose the iteration. Splitting it across start/stop would mean
        # holding a half-entered context, which is exactly the thing async context managers exist to prevent.
        self.log.info("mqtt.configured", host=self.host, port=self.port, topics=self.topics)

    async def stop(self) -> None:
        self._connected = False

    async def observations(self) -> AsyncIterator[Observation]:
        import aiomqtt

        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.host,
                    port=self.port,
                    identifier=self.client_id,
                    username=self.username,
                    password=self.password,
                    max_queued_incoming_messages=QUEUE_MAX,
                ) as client:
                    self._connected = True
                    self._error = None
                    for topic in self.topics:
                        await client.subscribe(topic, qos=self.qos)
                    self.log.info("mqtt.connected", topics=self.topics)

                    async for message in client.messages:
                        observation = self._to_observation(
                            str(message.topic), bytes(message.payload or b"")
                        )
                        if observation is not None:
                            yield observation
            except Exception as error:
                # Reconnect rather than die. A broker restart is routine, and a connector that gives up on the
                # first disconnect means every broker upgrade needs a platform restart too.
                self._connected = False
                self._error = f"{type(error).__name__}: {error}"
                self.log.warning("mqtt.disconnected", error=self._error)
                import asyncio

                await asyncio.sleep(5.0)

    def _to_observation(self, topic: str, payload: bytes) -> Observation | None:
        try:
            decoded = payload.decode()
        except UnicodeDecodeError:
            # Counted, not just logged. A sensor publishing protobuf onto a topic we thought was JSON produces
            # nothing but silence otherwise, and "the bridge is up and no data is arriving" is the hardest
            # thing to debug from outside.
            self._undecodable += 1
            self.log.warning("mqtt.undecodable", topic=topic, bytes=len(payload))
            return None

        try:
            body = json.loads(decoded)
        except json.JSONDecodeError:
            # A bare value, which plenty of sensors publish — `21.5` on `site/temp/3`. Wrapped rather than
            # rejected, because refusing it would rule out a large share of real MQTT deployments.
            body = {"value": _coerce(decoded)}

        if not isinstance(body, dict):
            body = {"value": body}

        self._received += 1
        latitude = _number(body.get(self.mapping.get("lat", "lat")))
        longitude = _number(body.get(self.mapping.get("lon", "lon")))
        return Observation(
            source_id=self.source_id,
            modality=self.modality,
            ts=utc_now(),
            geo=(
                Geo(lat=latitude, lon=longitude)
                if latitude is not None and longitude is not None
                else None
            ),
            # The topic travels in the payload. It is frequently the only place the sensor's identity or
            # location appears — `site/dock_3/temperature` says more than the body does.
            payload={
                "topic": topic,
                **body,
                "label": str(body.get(self.mapping.get("label", "name"), "") or "") or None,
            },
        )

    async def health(self) -> str:
        if self._error:
            return f"degraded: {self._error}"
        if not self._connected:
            return f"degraded: not connected to {self.host}:{self.port}"
        suffix = f", {self._undecodable} undecodable" if self._undecodable else ""
        return f"ok ({self._received} received{suffix})"

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "host": f"{self.host}:{self.port}",
            "topics": self.topics,
            "received": self._received,
            "undecodable": self._undecodable,
            "connected": self._connected,
        }


def _coerce(raw: str) -> Any:
    """Read a bare payload as the type it looks like.

    `"21.5"` from a sensor is a number, and leaving it a string means every downstream comparison silently
    becomes a string comparison — where `"9" > "10"`.
    """
    text = raw.strip()
    for cast in (int, float):
        with contextlib.suppress(ValueError):
            return cast(text)
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    return text


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = ["QUEUE_MAX", "MqttConnector"]
