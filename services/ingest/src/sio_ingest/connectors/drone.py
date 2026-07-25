"""MAVLink drone telemetry (PRD M1, Phase 7).

MAVLink is what real drones speak — ArduPilot, PX4, and DJI through a bridge. This connector reads a telemetry
stream and turns position reports into observations, which is what lets the platform track its own aircraft rather
than only detecting them in camera frames.

Two things make this connector unlike the others.

**MAVLink is not one message, it is forty.** A stream carries heartbeats, attitude, battery, GPS, mode changes and
status text, at different rates. Turning every message into an observation would produce a hundred a second of
which two matter. So this reads a **small whitelist** — position, battery, heartbeat — and merges them into one
observation per position report, carrying the most recent battery and mode alongside. A drone's position without
its battery level is half a fact when the question is "can it reach the fuel store and come back?"

**A drone is an entity the platform already tracks by other means.** The perception stack sees it in camera
frames, and fusion will be matching those tracks against this telemetry. So the observation carries the vehicle's
MAVLink system id in its payload and does *not* claim an `entity_id` — asserting identity here would let a
telemetry link overrule a track that the fusion service is better placed to reconcile.

Tested against a **fake MAVLink source** rather than SITL. SITL is the right thing for a pre-flight check and the
wrong thing for a test suite: it is a 200MB dependency, it takes 30 seconds to boot, and what it exercises is
ArduPilot's flight logic, not this file. The contract test drives a fake connection object and asserts the
message-to-observation mapping, which is the part that can actually be wrong here.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from typing import Any

from sio_schemas import Geo, Modality, Observation, Velocity, utc_now

from .base import Connector, ConnectorConfig, register_connector

#: MAVLink messages worth reading.
#:
#: A whitelist, because a stream carries forty types at different rates and turning all of them into observations
#: would produce a hundred a second of which two matter.
INTERESTING = frozenset(
    {
        "GLOBAL_POSITION_INT",  # fused position, the one that matters
        "GPS_RAW_INT",  # fallback when there is no EKF solution yet
        "SYS_STATUS",  # battery
        "HEARTBEAT",  # mode and armed state
        "STATUSTEXT",  # the autopilot complaining, which is worth logging
    }
)

#: MAVLink sends degrees as int * 1e7 and millimetres for altitude. Named, because a bare `1e7` in the middle of
#: a coordinate conversion is how a drone ends up in the Gulf of Guinea.
DEGREES_SCALE = 1e7
MM_PER_M = 1000.0
CM_PER_M = 100.0


@register_connector
class MavlinkDroneConnector(Connector):
    """Reads MAVLink telemetry and yields position observations.

    Optional dependency (`pymavlink`). The connection string is passed through to `mavutil.mavlink_connection`,
    so anything pymavlink accepts works: `udpin:0.0.0.0:14550` for a vehicle broadcasting at you,
    `tcp:127.0.0.1:5760` for SITL, `/dev/ttyUSB0` for a radio.
    """

    kind = "drone_mavlink"
    # GPS, not a `DRONE` modality — there isn't one, and inventing one would be wrong anyway:
    # what this connector produces is a position report. The vehicle is identified in the
    # payload, and `source_id` says which link it came over.
    modality = Modality.GPS

    def __init__(self, config: ConnectorConfig) -> None:
        super().__init__(config)
        options = config.options
        self.connection_string = str(options.get("connection", "udpin:0.0.0.0:14550"))
        self.source_system = int(options.get("source_system", 255))
        # Position reports arrive far faster than the platform needs. Throttled to 2Hz by default: a yard drone
        # at 10m/s moves 5m between reports, which is well inside the resolution anything downstream cares about.
        self.min_interval_s = float(options.get("min_interval_s", 0.5))
        self.label = config.label or str(options.get("label", "drone"))
        self._connection: Any = None
        self._last_emit = 0.0
        self._battery_percent: float | None = None
        self._mode: str | None = None
        self._armed: bool | None = None
        self._messages = 0
        self._emitted = 0
        self._error: str | None = None

    async def start(self) -> None:
        try:
            from pymavlink import mavutil
        except ImportError as error:
            raise RuntimeError(
                "drone_mavlink needs pymavlink: `uv pip install 'sio-ingest[drone]'`. Optional because "
                "most deployments have no aircraft, and the dependency pulls in a MAVLink dialect "
                "generator that takes a while to build."
            ) from error

        # On a thread: `mavlink_connection` binds a socket and, for some transports, blocks waiting for the
        # first packet. Doing that on the event loop would stall every other connector in the service.
        self._connection = await asyncio.to_thread(
            mavutil.mavlink_connection,
            self.connection_string,
            source_system=self.source_system,
        )
        self.log.info("mavlink.listening", connection=self.connection_string)

    async def stop(self) -> None:
        if self._connection is not None:
            with contextlib.suppress(Exception):
                self._connection.close()
            self._connection = None

    async def observations(self) -> AsyncIterator[Observation]:
        while True:
            message = await asyncio.to_thread(self._recv)
            if message is None:
                # No traffic within the timeout. Not an error — a drone on the ground with its radio off is a
                # normal state, and logging it would fill the log with the absence of news.
                continue
            observation = self._handle(message)
            if observation is not None:
                yield observation

    def _recv(self) -> Any:
        """One message, blocking, on a thread.

        A timeout rather than blocking for ever, so `stop()` is not waiting on a drone that has gone quiet.
        """
        try:
            return self._connection.recv_match(  # type: ignore[union-attr]
                type=list(INTERESTING), blocking=True, timeout=2.0
            )
        except Exception as error:
            self._error = f"{type(error).__name__}: {error}"
            return None

    def _handle(self, message: Any) -> Observation | None:
        """Update state from any interesting message; emit only on a position.

        Battery and mode are *state*, not events: they change slowly and matter as context on a position report.
        Emitting an observation for each one would triple the volume and tell nobody anything new.
        """
        self._messages += 1
        kind = message.get_type()

        if kind == "SYS_STATUS":
            remaining = getattr(message, "battery_remaining", -1)
            # -1 means "not reported", which is not 0% — and treating it as 0 would produce a fleet of drones
            # that all appear to be about to fall out of the sky.
            self._battery_percent = float(remaining) if remaining >= 0 else None
            return None

        if kind == "HEARTBEAT":
            self._mode = _mode_of(message)
            base_mode = getattr(message, "base_mode", 0)
            # Bit 7 of base_mode is MAV_MODE_FLAG_SAFETY_ARMED.
            self._armed = bool(base_mode & 0b1000_0000)
            return None

        if kind == "STATUSTEXT":
            text = str(getattr(message, "text", "")).strip()
            if text:
                # Logged, not ingested. The autopilot's own words are invaluable when something goes wrong and
                # noise in an observation stream the rest of the time.
                self.log.info(
                    "mavlink.statustext", text=text, severity=getattr(message, "severity", None)
                )
            return None

        if kind not in ("GLOBAL_POSITION_INT", "GPS_RAW_INT"):
            return None

        # , not the event loop's clock. `asyncio.get_event_loop()` is deprecated outside a
        # running loop and a throttle has no business depending on one — this method is pure message handling
        # and is far easier to test without a loop in scope.
        now = time.monotonic()
        if now - self._last_emit < self.min_interval_s:
            return None
        self._last_emit = now

        latitude = getattr(message, "lat", None)
        longitude = getattr(message, "lon", None)
        if latitude in (None, 0) and longitude in (None, 0):
            # 0,0 is what an autopilot reports before it has a fix. Null Island is not a place any drone is,
            # and letting it through would put an entity in the Atlantic and skew every spatial query.
            return None

        geo = Geo(
            lat=float(latitude) / DEGREES_SCALE,
            lon=float(longitude) / DEGREES_SCALE,
            alt=_altitude_of(message),
        )
        velocity = _velocity_of(message)
        self._emitted += 1
        self._error = None
        return Observation(
            source_id=self.source_id,
            modality=self.modality,
            ts=utc_now(),
            geo=geo,
            payload={
                # Velocity in the payload: `Observation` has no velocity field, deliberately — it is a raw
                # signal envelope, and a tracker derives velocity from a sequence of positions rather than
                # trusting one source's own estimate. The autopilot's figure is still worth carrying, since it
                # is measured rather than differenced.
                "velocity_ms": (
                    {"north": velocity.north, "east": velocity.east, "up": velocity.up}
                    if velocity
                    else None
                ),
                # The MAVLink system id, so fusion can reconcile this telemetry with camera tracks. NOT an
                # entity_id: asserting identity here would let a radio link overrule a track that fusion is
                # better placed to judge.
                "mavlink_system": message.get_srcSystem(),
                "message": kind,
                "battery_percent": self._battery_percent,
                "mode": self._mode,
                "armed": self._armed,
                "relative_alt_m": _relative_altitude_of(message),
                "heading_deg": _heading_of(message),
                "label": self.label,
            },
        )

    async def health(self) -> str:
        if self._error:
            return f"degraded: {self._error}"
        if self._messages == 0:
            return f"degraded: no MAVLink traffic on {self.connection_string}"
        battery = (
            f", battery {self._battery_percent:.0f}%" if self._battery_percent is not None else ""
        )
        return f"ok ({self._emitted} positions from {self._messages} messages{battery})"

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "connection": self.connection_string,
            "messages": self._messages,
            "positions": self._emitted,
            "battery_percent": self._battery_percent,
            "mode": self._mode,
            "armed": self._armed,
        }


def _altitude_of(message: Any) -> float | None:
    """Altitude in metres above mean sea level.

    Both message types use millimetres, and `GPS_RAW_INT` calls it `alt` too — so one conversion covers both.
    """
    raw = getattr(message, "alt", None)
    return float(raw) / MM_PER_M if raw is not None else None


def _relative_altitude_of(message: Any) -> float | None:
    """Height above the launch point, which is the number a pilot actually flies by.

    Only `GLOBAL_POSITION_INT` carries it. Kept separate from `alt` rather than replacing it: "300m above sea
    level" and "50m above the yard" are different facts and a geofence needs the second one.
    """
    raw = getattr(message, "relative_alt", None)
    return float(raw) / MM_PER_M if raw is not None else None


def _velocity_of(message: Any) -> Velocity | None:
    """Ground velocity in m/s, converted from MAVLink's NED centimetres-per-second.

    Two conversions, and the second one is a sign flip that matters:

    * MAVLink `GLOBAL_POSITION_INT` sends `vx`/`vy`/`vz` in **cm/s**, in the **NED** frame — north, east, DOWN.
    * This platform's `Velocity` is `north`/`east`/`UP`.

    So `up = -vz`. Without the negation a descending drone reads as climbing, which is exactly the sort of error
    that survives every test that only checks magnitudes — and it would have shipped, because my first version
    passed `vx=`/`vy=`/`vz=` to a model whose fields are named `north`/`east`/`up` and Pydantic rejected the
    field names before it ever got as far as the sign.

    `GPS_RAW_INT` carries no velocity, so this returns `None` for it rather than fabricating a zero: a
    stationary reading and an absent one must not look the same to a tracker.
    """
    vx = getattr(message, "vx", None)
    vy = getattr(message, "vy", None)
    if vx is None or vy is None:
        return None
    return Velocity(
        north=float(vx) / CM_PER_M,
        east=float(vy) / CM_PER_M,
        # Negated: MAVLink is positive DOWN, this schema is positive UP.
        up=-float(getattr(message, "vz", 0) or 0) / CM_PER_M,
    )


def _heading_of(message: Any) -> float | None:
    """Heading in degrees. MAVLink sends centidegrees, and 65535 means "unknown"."""
    raw = getattr(message, "hdg", None)
    if raw is None or raw == 65535:
        return None
    return float(raw) / 100.0


def _mode_of(message: Any) -> str | None:
    """The flight mode name, when pymavlink can resolve it.

    Wrapped in a try because the mapping depends on the autopilot type in the heartbeat, and a vehicle type
    pymavlink does not know raises rather than returning None.
    """
    with contextlib.suppress(Exception):
        from pymavlink import mavutil

        return str(
            mavutil.mode_string_v10(message)  # type: ignore[attr-defined]
        )
    custom = getattr(message, "custom_mode", None)
    return f"mode_{custom}" if custom is not None else None


__all__ = [
    "CM_PER_M",
    "DEGREES_SCALE",
    "INTERESTING",
    "MM_PER_M",
    "MavlinkDroneConnector",
]
