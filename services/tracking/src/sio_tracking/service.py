"""Tracking service: detections in, tracks out (PRD M4)."""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI

from sio_core import MessageContext, SioService
from sio_core.ports import VisionResult
from sio_schemas import (
    BusMessage,
    Detection,
    Geo,
    Topic,
    Track,
    Velocity,
    utc_now,
)
from sio_schemas import (
    TrackState as SchemaTrackState,
)
from sio_schemas import (
    TrackState as _State,  # noqa: F401 - kept for clarity of the two TrackState names
)
from sio_schemas.perception import TrackState as TrackStatePoint

from .bytetrack import ByteTracker, TrackState, displacement
from .bytetrack import Track as InternalTrack
from .crosscam import CrossCameraAssociator


class TrackingService(SioService):
    """Maintains one :class:`ByteTracker` per camera and publishes the tracks it produces.

    **One tracker per source, always.** Track ids are identities in a single camera's image space;
    feeding two cameras' detections to one tracker asks it to associate boxes that share no
    coordinate frame, and it will happily do so — producing tracks that teleport across the site.
    Cross-camera identity is a separate problem, solved separately in :mod:`crosscam`.

    Detections arrive per object, not per frame, so the service batches by ``(source, frame)`` and
    steps a tracker only when a frame's worth has arrived or a short flush window expires. Stepping
    per detection would advance the Kalman filter once per object and destroy the motion model.
    """

    name = "tracking"
    subscribes = (Topic.DETECTIONS,)
    tick_interval_s = 30.0

    FLUSH_AFTER_S = 0.35
    """How long to wait for the rest of a frame's detections before stepping the tracker.

    Longer than the pipeline's per-frame spread, shorter than the frame interval at 2 fps.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.trackers: dict[str, ByteTracker] = {}
        self.crosscam = CrossCameraAssociator(
            reid_threshold=self.settings.track_reid_threshold,
            enabled=self.settings.enable_cross_camera,
        )
        self._pending: dict[str, list[Detection]] = {}
        self._pending_since: dict[str, float] = {}
        self._published = 0
        self._frames_stepped = 0
        self._track_ids_created = 0

    # ----------------------------------------------------------------- lifecycle
    async def setup(self) -> None:
        self.log.info(
            "tracking.ready",
            iou_threshold=self.settings.track_iou_threshold,
            reid_threshold=self.settings.track_reid_threshold,
            max_age=self.settings.track_max_age,
            min_hits=self.settings.track_min_hits,
            cross_camera=self.settings.enable_cross_camera,
        )

    def _tracker_for(self, source_id: str) -> ByteTracker:
        if source_id not in self.trackers:
            self.trackers[source_id] = ByteTracker(
                iou_threshold=self.settings.track_iou_threshold,
                reid_threshold=self.settings.track_reid_threshold,
                max_age=self.settings.track_max_age,
                min_hits=self.settings.track_min_hits,
            )
            self.log.info("tracking.tracker_created", source=source_id)
        return self.trackers[source_id]

    # ------------------------------------------------------------------ handling
    async def on_message(self, message: BusMessage, ctx: MessageContext) -> None:
        if message.kind != "Detection":
            return
        detection = message.decode(Detection)
        if detection.bbox is None:
            return  # a detection with no box (an audio event) is not trackable

        # Group by frame. The observation id identifies the frame, which is what makes this correct
        # even when detections from two cameras interleave on the bus.
        key = f"{detection.source_id}|{detection.observation_id}"
        self._pending.setdefault(key, []).append(detection)
        self._pending_since.setdefault(key, time.monotonic())

        await self._flush_ready(ctx)

    async def tick(self) -> None:
        """Flush stragglers and report.

        Without this a frame whose last detection never arrives — the final frame before a camera
        goes quiet — would sit in the buffer forever, and its track update would never happen.
        """
        await self._flush_ready(None, force=True)
        self.log.info(
            "tracking.stats",
            sources=len(self.trackers),
            frames_stepped=self._frames_stepped,
            tracks_published=self._published,
            ids_created=self._track_ids_created,
            reid_recoveries=sum(t.reid_recoveries for t in self.trackers.values()),
            cross_camera_links=self.crosscam.link_count,
        )

    async def _flush_ready(self, ctx: MessageContext | None, *, force: bool = False) -> None:
        now = time.monotonic()
        ready = [
            key
            for key, since in self._pending_since.items()
            if force or now - since >= self.FLUSH_AFTER_S
        ]
        for key in ready:
            detections = self._pending.pop(key, [])
            self._pending_since.pop(key, None)
            if detections:
                await self._step(detections, ctx)

    async def _step(self, detections: list[Detection], ctx: MessageContext | None) -> None:
        """Advance one camera's tracker by one frame and publish the resulting tracks."""
        source_id = detections[0].source_id
        tracker = self._tracker_for(source_id)
        before = tracker.stats()["next_id"]

        results = [self._to_vision_result(detection) for detection in detections]
        active = tracker.update(results)
        self._frames_stepped += 1
        self._track_ids_created += tracker.stats()["next_id"] - before

        for internal in active:
            if internal.state is TrackState.TENTATIVE:
                # Do not publish an unconfirmed track: a single spurious detection would create an
                # entity in the world model, and a world model full of one-frame ghosts is worse than
                # one that is a few frames behind.
                continue
            track = self._to_schema_track(internal, detections[0])
            links = self.crosscam.observe(source_id, internal, track)
            if links:
                track.cross_camera_of = links
            if ctx is not None:
                await ctx.publish(Topic.TRACKS, track)
            else:
                await self.publish(Topic.TRACKS, track)
            self._published += 1

    @staticmethod
    def _to_vision_result(detection: Detection) -> VisionResult:
        """Rebuild the detector's output shape from the envelope the bus carries."""
        embedding = detection.attrs.get("embedding")
        return VisionResult(
            label=detection.class_name,
            confidence=detection.confidence,
            bbox=detection.bbox,  # type: ignore[arg-type]
            mask_rle=detection.mask_ref,
            embedding=tuple(embedding) if isinstance(embedding, list) else None,
            attrs=detection.attrs,
        )

    def _to_schema_track(self, internal: InternalTrack, sample: Detection) -> Track:
        """Convert an internal track into the published envelope."""
        states = [
            TrackStatePoint(
                ts=sample.ts,
                bbox=historic,
                confidence=internal.confidence,
                detection_id=sample.id if index == len(internal.history) - 1 else None,
            )
            for index, historic in enumerate(internal.history[-30:])
        ]
        return Track(
            track_id=f"trk-{sample.source_id}-{internal.track_id}",
            tenant_id=sample.tenant_id,
            trace_id=sample.trace_id,
            **{"class": internal.label},
            states=states,
            confidence=internal.confidence,
            source_id=sample.source_id,
            status=SchemaTrackState.CONFIRMED
            if internal.state is TrackState.CONFIRMED
            else SchemaTrackState.LOST,
            start_ts=sample.ts,
            last_ts=sample.ts,
            hits=internal.hits,
            age=internal.age,
            time_since_update=internal.time_since_update,
            embedding=internal.embedding.tolist() if internal.embedding is not None else None,
        )

    # -------------------------------------------------------------------- routes
    def routes(self, app: FastAPI) -> None:
        @app.get("/tracks", tags=["tracking"])
        async def current_tracks() -> dict[str, Any]:
            """What each camera is tracking right now — the fastest way to see if ids are stable."""
            return {
                source: {
                    "stats": tracker.stats(),
                    "tracks": [
                        {
                            "track_id": track.track_id,
                            "class": track.label,
                            "state": str(track.state),
                            "hits": track.hits,
                            "age": track.age,
                            "confidence": round(track.confidence, 3),
                            "bbox": track.box.to_wire(),
                            "path_px": round(displacement(track.history), 1),
                            "has_embedding": track.embedding is not None,
                        }
                        for track in tracker.tracks
                    ],
                }
                for source, tracker in self.trackers.items()
            }

        @app.get("/cross-camera", tags=["tracking"])
        async def cross_camera() -> dict[str, Any]:
            return self.crosscam.describe()

    async def health_info(self) -> dict[str, str]:
        return {
            "sources": str(len(self.trackers)),
            "frames_stepped": str(self._frames_stepped),
            "tracks_published": str(self._published),
            "ids_created": str(self._track_ids_created),
            "pending_frames": str(len(self._pending)),
        }


def geo_from_track(track: InternalTrack, projector: Any) -> Geo | None:
    """Project a track's image-space box to a ground position, if a projector is available.

    Image-to-ground projection needs camera calibration, which the spatial engine owns (Phase 3).
    Kept as a seam here so tracking does not grow a second responsibility.
    """
    if projector is None:
        return None
    return projector(track.box)


def velocity_from_track(
    track: InternalTrack, *, metres_per_pixel: float | None = None
) -> Velocity | None:
    """Convert pixel velocity to ground velocity when a scale is known."""
    if metres_per_pixel is None:
        return None
    vx, vy = track.filter.velocity
    return Velocity(east=vx * metres_per_pixel, north=-vy * metres_per_pixel)


__all__ = ["TrackingService", "geo_from_track", "utc_now", "velocity_from_track"]
