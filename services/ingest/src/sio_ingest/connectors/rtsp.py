"""RTSP camera connector (PRD M1, Phase 7).

The connector a real deployment needs first: an actual camera on the wall, pushing H.264 over RTSP.

**Frames go to object storage, not into the message bus.** A 1080p JPEG is ~200KB, and at 5 cameras at 5fps that
is 5MB/s of message payload — which would work for about a day and then fall over in a way that looks like Redis
being slow. The observation carries a blob key; the perception service already reads frames from there.

**It decimates on purpose.** A camera at 25fps is 25 chances per second to run a detector that takes 80ms, and
the naive version falls behind immediately and then reports the world as it was thirty seconds ago — the worst
possible failure for a live platform, because the map looks fine. This connector reads every frame (it must, or
the decoder desynchronises) and *publishes* at a configured rate, dropping the rest before they cost anything.

**Two backends, chosen at runtime.** OpenCV is the pragmatic default: it is one dependency, it handles RTSP over
TCP, and it works. GStreamer is offered for the case OpenCV handles badly — hardware decode, and multi-camera
setups where FFmpeg's threading becomes the bottleneck. The seam is here rather than in a rewrite later.

Tested against a **loopback stream** rather than a real camera: `just rtsp-loopback` serves a generated pattern
over RTSP on localhost, and the contract test asserts frames arrive, get stored, and are decimated to the
configured rate. What that exercises is this file's logic, which is the part that can be wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from typing import Any

from sio_schemas import Modality, Observation, utc_now

from .base import Connector, ConnectorConfig, register_connector

#: JPEG quality for stored frames.
#:
#: 85 is the point where artefacts stop being visible to a detector. Higher wastes storage on a frame nobody will
#: look at twice; lower starts costing recall on small objects, which is the opposite of the point.
JPEG_QUALITY = 85

#: How many consecutive read failures before the stream is considered dead and reopened.
#:
#: A few dropped frames is a network hiccup; thirty in a row is a camera that has rebooted. Reconnecting on the
#: first failure would thrash on a lossy link, and never reconnecting means one hiccup ends the day.
MAX_READ_FAILURES = 30


@register_connector
class RtspCameraConnector(Connector):
    """Pulls frames from an RTSP camera, stores them, and yields references.

    Optional dependency (`opencv-python-headless`). Headless deliberately: the GUI build pulls in Qt and X11,
    which on a server is 200MB of nothing useful and on macOS occasionally breaks in ways that look like a
    codec problem.
    """

    kind = "camera_rtsp"
    modality = Modality.VIDEO

    def __init__(self, config: ConnectorConfig) -> None:
        super().__init__(config)
        options = config.options
        self.url = str(options.get("url", ""))
        self.backend = str(options.get("backend", "opencv"))
        # The rate frames are PUBLISHED at, not the rate they are read at. 2fps is plenty for a yard: a truck at
        # 20km/h moves under 3m between frames, and the detector costs 80ms a frame on CPU.
        self.publish_fps = float(options.get("publish_fps", config.rate_hz or 2.0))
        self.width = int(options.get("width", 0)) or None
        self.transport = str(options.get("transport", "tcp"))
        self.store_frames = bool(options.get("store_frames", True))
        self._capture: Any = None
        self._store: Any = None
        self._read = 0
        self._published = 0
        self._failures = 0
        self._reconnects = 0
        self._error: str | None = None
        self._last_publish = 0.0

    @property
    def min_interval_s(self) -> float:
        return 1.0 / self.publish_fps if self.publish_fps > 0 else 0.0

    async def start(self) -> None:
        if not self.url:
            raise ValueError(f"{self.kind} needs options.url (an rtsp:// address)")
        if self.backend not in ("opencv", "gstreamer"):
            raise ValueError(
                f"unknown backend {self.backend!r}; use 'opencv' (the default) or 'gstreamer'"
            )
        if self.store_frames:
            from sio_core import get_blob

            self._store = get_blob()
        await self._open()

    async def _open(self) -> None:
        try:
            import cv2
        except ImportError as error:
            raise RuntimeError(
                "camera_rtsp needs OpenCV: `uv pip install 'sio-ingest[camera]'`. Optional because a "
                "deployment with no cameras should not carry a video decoder, and the headless wheel is "
                "still 60MB."
            ) from error

        # RTSP over TCP by default. UDP loses packets and OpenCV's recovery from that is to produce green
        # smears, which a detector then reports as objects — a failure mode that looks like a model problem.
        import os

        # SIO-ENV-OK: an environment variable is FFmpeg's ONLY channel for the RTSP transport — there is no
        # VideoCapture property for it. The value comes from `options.transport`, so the configuration arrives
        # the proper way; this writes a third-party library's knob rather than reading our own config.
        os.environ.setdefault(  # SIO-ENV-OK
            "OPENCV_FFMPEG_CAPTURE_OPTIONS", f"rtsp_transport;{self.transport}"
        )

        target = self.url if self.backend == "opencv" else self._gst_pipeline()
        api = cv2.CAP_FFMPEG if self.backend == "opencv" else cv2.CAP_GSTREAMER
        # On a thread: opening an RTSP stream negotiates with the camera and can take seconds.
        self._capture = await asyncio.to_thread(cv2.VideoCapture, target, api)
        if not self._capture.isOpened():
            raise RuntimeError(
                f"could not open {self.url}. Check the URL, that the camera is reachable, and that "
                f"the transport is right — some cameras only serve RTSP over UDP "
                f"(set options.transport to 'udp')."
            )
        # A small buffer. The default lets OpenCV queue frames, and reading from a queue means reading the PAST:
        # the connector would report positions several seconds stale while appearing perfectly healthy.
        with contextlib.suppress(Exception):
            self._capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.log.info("rtsp.opened", url=_redact(self.url), backend=self.backend)

    def _gst_pipeline(self) -> str:
        """A GStreamer pipeline string for the hardware-decode path.

        `latency=0` and `drop-on-latency` matter: the defaults buffer for smooth playback, which is right for a
        video player and wrong for a platform that wants the newest frame and nothing else.
        """
        return (
            f"rtspsrc location={self.url} latency=0 drop-on-latency=true protocols={self.transport} "
            f"! rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! appsink drop=true max-buffers=1"
        )

    async def stop(self) -> None:
        if self._capture is not None:
            with contextlib.suppress(Exception):
                self._capture.release()
            self._capture = None

    async def observations(self) -> AsyncIterator[Observation]:
        while True:
            frame = await asyncio.to_thread(self._read_frame)
            if frame is None:
                if self._failures >= MAX_READ_FAILURES:
                    await self._reconnect()
                continue
            observation = await self._maybe_publish(frame)
            if observation is not None:
                yield observation

    def _read_frame(self) -> Any:
        """Read one frame. Every frame, even the ones that will be dropped.

        Reading every frame is not optional: skipping reads leaves the decoder's buffer to fill and the stream
        desynchronises, which shows up as corrupt frames rather than as missing ones. Decimation happens AFTER
        the read, where it costs only an encode we skip.
        """
        if self._capture is None:
            return None
        ok, frame = self._capture.read()
        if not ok:
            self._failures += 1
            return None
        self._failures = 0
        self._read += 1
        return frame

    async def _reconnect(self) -> None:
        self._reconnects += 1
        self._error = f"stream dropped after {MAX_READ_FAILURES} failed reads; reopening"
        self.log.warning("rtsp.reconnecting", url=_redact(self.url), attempt=self._reconnects)
        await self.stop()
        # A fixed pause rather than exponential backoff: a camera reboots in about ten seconds, and backing off
        # to minutes means a camera that recovered stays dark because we stopped asking.
        await asyncio.sleep(3.0)
        with contextlib.suppress(Exception):
            await self._open()
            self._failures = 0
            self._error = None

    async def _maybe_publish(self, frame: Any) -> Observation | None:
        now = time.monotonic()
        if now - self._last_publish < self.min_interval_s:
            return None
        self._last_publish = now

        encoded, dimensions = await asyncio.to_thread(self._encode, frame)
        if encoded is None:
            return None

        key: str | None = None
        if self._store is not None:
            key = f"frames/{self.source_id}/{utc_now().strftime('%Y%m%dT%H%M%S%f')}.jpg"
            try:
                await self._store.put(key, encoded, content_type="image/jpeg")
            except Exception as error:
                # A storage failure must not stop the stream. The frame is lost; the camera being up is still
                # worth reporting, and the health line will say storage is broken.
                self._error = f"frame store failed: {type(error).__name__}: {error}"
                self.log.warning("rtsp.store_failed", error=self._error)
                key = None

        self._published += 1
        return Observation(
            source_id=self.source_id,
            modality=self.modality,
            ts=utc_now(),
            # `raw_ref`, NOT a payload key. The perception service reads `observation.raw_ref` and skips
            # any observation without one — so putting the key in `payload` would have meant perception never
            # processed a single real camera frame, silently, while every counter in this connector said it was
            # healthy. A reference rather than the bytes because 200KB per frame through the bus is 5MB/s
            # across five cameras, which works for about a day and then looks like Redis being slow.
            raw_ref=key,
            payload={
                "width": dimensions[0],
                "height": dimensions[1],
                "frames_read": self._read,
                "publish_fps": self.publish_fps,
                "camera_url": _redact(self.url),
                "label": self.config.label or self.source_id,
            },
        )

    def _encode(self, frame: Any) -> tuple[bytes | None, tuple[int, int]]:
        import cv2

        if self.width and frame.shape[1] > self.width:
            # Downscale before encoding, not after: encoding a 4K frame to throw most of it away costs ~40ms
            # per frame, which at five cameras is a core.
            scale = self.width / frame.shape[1]
            frame = cv2.resize(frame, (self.width, int(frame.shape[0] * scale)))
        height, width = frame.shape[:2]
        ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        return (buffer.tobytes() if ok else None), (width, height)

    async def health(self) -> str:
        if self._error:
            return f"degraded: {self._error}"
        if self._capture is None:
            return f"degraded: {_redact(self.url)} is not open"
        suffix = f", {self._reconnects} reconnects" if self._reconnects else ""
        return f"ok ({self._published} published from {self._read} read{suffix})"

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            # Redacted, because RTSP URLs carry credentials inline and `describe()` goes to `/health`, which is
            # the least private endpoint the service has.
            "url": _redact(self.url),
            "backend": self.backend,
            "publish_fps": self.publish_fps,
            "frames_read": self._read,
            "frames_published": self._published,
            "reconnects": self._reconnects,
        }


def _redact(url: str) -> str:
    """Strip credentials from an RTSP URL.

    `rtsp://admin:hunter2@10.0.0.5/stream` is the normal form for a camera, and that string ends up in logs,
    health payloads and error messages. Redacting at the boundary is easier than remembering not to log it.
    """
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}" if scheme else f"***@{host}"


__all__ = ["JPEG_QUALITY", "MAX_READ_FAILURES", "RtspCameraConnector"]
