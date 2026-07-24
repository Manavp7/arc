"""ByteTrack: multi-object tracking by association (PRD M4).

Implemented here rather than pulled from BoxMOT because BoxMOT's install path drags in PyTorch, and
this is about 250 lines of Kalman filtering and assignment. The `Tracker` port keeps BoxMOT and
DeepStream MV3DT as drop-in alternatives.

What makes ByteTrack work, and what this implementation preserves: **low-confidence detections are
not thrown away**. The first association pass matches confident detections to tracks; the second pass
offers the *leftover tracks* the low-confidence detections. A partially occluded truck produces a
weak detection, and a tracker that discards it loses the identity and then invents a new one when the
truck reappears — which is exactly the failure that makes dwell times and journey histories useless.

Appearance is used as a tie-breaker rather than a primary signal. ReID embeddings recover an identity
across a gap where IoU is zero, but geometry is the stronger cue frame to frame, and appearance alone
confuses two identical white vans parked side by side.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import pairwise
from typing import Any

import numpy as np

from sio_core import get_logger
from sio_core.ports import VisionResult
from sio_schemas import BBox

log = get_logger("sio.tracking.bytetrack")


class TrackState(StrEnum):
    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"
    LOST = "lost"
    REMOVED = "removed"


class KalmanBoxFilter:
    """Constant-velocity Kalman filter on ``[cx, cy, aspect, height]``.

    Tracking the centre, aspect ratio and height — rather than the four corners — is what lets the
    filter model *motion* separately from *apparent size*. A truck driving away from the camera moves
    smoothly in centre and shrinks smoothly in height; a corner parameterisation couples those and
    produces a filter that fights itself.

    Hand-rolled because the whole thing is four lines of matrix algebra and the alternative is a
    dependency for a 4-state linear filter.
    """

    def __init__(self, box: BBox, *, dt: float = 1.0) -> None:
        cx, cy = box.center
        aspect = box.width / max(box.height, 1e-6)
        self.mean = np.array([cx, cy, aspect, box.height, 0.0, 0.0, 0.0, 0.0], dtype=np.float64)
        # Initial uncertainty: position is known reasonably well, velocity not at all.
        self.covariance = np.diag([10.0, 10.0, 1e-2, 10.0, 1e3, 1e3, 1e-4, 1e3]).astype(np.float64)

        self._motion = np.eye(8)
        for index in range(4):
            self._motion[index, index + 4] = dt
        self._observation = np.eye(4, 8)
        # Process noise: generous on velocity so the filter can follow a turn, tight on aspect
        # because an object's shape does not change abruptly.
        self._process_noise = np.diag([1.0, 1.0, 1e-4, 1.0, 10.0, 10.0, 1e-6, 10.0])
        self._measurement_noise = np.diag([1.0, 1.0, 1e-2, 1.0])

    def predict(self) -> None:
        self.mean = self._motion @ self.mean
        self.covariance = self._motion @ self.covariance @ self._motion.T + self._process_noise

    def update(self, box: BBox) -> None:
        cx, cy = box.center
        measurement = np.array(
            [cx, cy, box.width / max(box.height, 1e-6), box.height], dtype=np.float64
        )
        residual = measurement - self._observation @ self.mean
        projected = (
            self._observation @ self.covariance @ self._observation.T + self._measurement_noise
        )
        gain = self.covariance @ self._observation.T @ np.linalg.inv(projected)
        self.mean = self.mean + gain @ residual
        identity = np.eye(8)
        self.covariance = (identity - gain @ self._observation) @ self.covariance

    @property
    def box(self) -> BBox:
        cx, cy, aspect, height = self.mean[:4]
        height = max(float(height), 1.0)
        width = max(float(aspect) * height, 1.0)
        return BBox(
            x1=max(0.0, cx - width / 2),
            y1=max(0.0, cy - height / 2),
            x2=max(1.0, cx + width / 2),
            y2=max(1.0, cy + height / 2),
        )

    @property
    def velocity(self) -> tuple[float, float]:
        return float(self.mean[4]), float(self.mean[5])


@dataclass
class Track:
    """One tracked object, in one camera's image space."""

    track_id: int
    label: str
    filter: KalmanBoxFilter
    confidence: float
    state: TrackState = TrackState.TENTATIVE
    hits: int = 1
    age: int = 0
    time_since_update: int = 0
    start_frame: int = 0
    last_frame: int = 0
    embedding: np.ndarray | None = None
    history: list[BBox] = field(default_factory=list)
    entity_id: str | None = None

    @property
    def box(self) -> BBox:
        return self.filter.box

    def predict(self) -> None:
        self.filter.predict()
        self.age += 1
        self.time_since_update += 1

    def update(self, detection: VisionResult, frame_index: int, *, min_hits: int) -> None:
        self.filter.update(detection.bbox)
        self.confidence = detection.confidence
        self.hits += 1
        self.time_since_update = 0
        self.last_frame = frame_index
        self.history.append(detection.bbox)
        if len(self.history) > 120:
            del self.history[:-120]
        if detection.embedding is not None:
            self._blend_embedding(np.asarray(detection.embedding, dtype=np.float32))
        if (
            self.state is not TrackState.CONFIRMED and self.hits >= min_hits
        ) or self.state is TrackState.LOST:
            self.state = TrackState.CONFIRMED

    def _blend_embedding(self, vector: np.ndarray, *, momentum: float = 0.9) -> None:
        """Exponentially smooth the appearance vector.

        A single frame's embedding is noisy — motion blur, a partial occlusion, a bad crop. Smoothing
        keeps a stable identity signature, and momentum near 1 means one bad crop cannot poison it.
        """
        if self.embedding is None:
            self.embedding = vector
            return
        blended = momentum * self.embedding + (1.0 - momentum) * vector
        norm = float(np.linalg.norm(blended))
        self.embedding = blended / norm if norm > 0 else blended

    def mark_lost(self) -> None:
        if self.state is not TrackState.REMOVED:
            self.state = TrackState.LOST


def iou_matrix(tracks: Sequence[Track], detections: Sequence[VisionResult]) -> np.ndarray:
    """Pairwise IoU between predicted track boxes and detections."""
    matrix = np.zeros((len(tracks), len(detections)), dtype=np.float32)
    for row, track in enumerate(tracks):
        box = track.box
        for column, detection in enumerate(detections):
            matrix[row, column] = box.iou(detection.bbox)
    return matrix


def cosine_matrix(tracks: Sequence[Track], detections: Sequence[VisionResult]) -> np.ndarray:
    """Pairwise appearance similarity, 0 where either side has no embedding."""
    matrix = np.zeros((len(tracks), len(detections)), dtype=np.float32)
    for row, track in enumerate(tracks):
        if track.embedding is None:
            continue
        for column, detection in enumerate(detections):
            if detection.embedding is None:
                continue
            matrix[row, column] = float(
                np.dot(track.embedding, np.asarray(detection.embedding, dtype=np.float32))
            )
    return matrix


def greedy_assign(cost: np.ndarray, threshold: float) -> list[tuple[int, int]]:
    """Greedy one-to-one assignment on a similarity matrix, best pair first.

    Greedy rather than Hungarian: with a handful of tracks per camera the optimal assignment and the
    greedy one almost always agree, and greedy is trivial to reason about when a match looks wrong.
    Swapping in ``scipy.optimize.linear_sum_assignment`` would be a five-line change if a site ever
    needed it.
    """
    matches: list[tuple[int, int]] = []
    if cost.size == 0:
        return matches
    working = cost.copy()
    while True:
        row, column = np.unravel_index(np.argmax(working), working.shape)
        best = working[row, column]
        if best < threshold:
            break
        matches.append((int(row), int(column)))
        working[row, :] = -1.0
        working[:, column] = -1.0
    return matches


class ByteTracker:
    """Two-stage association tracker for one camera.

    One instance per source: track ids are image-space identities, and mixing two cameras' detections
    into one tracker would try to associate objects that share no coordinate frame.
    """

    def __init__(
        self,
        *,
        high_threshold: float = 0.5,
        low_threshold: float = 0.1,
        iou_threshold: float = 0.3,
        reid_threshold: float = 0.75,
        max_age: int = 30,
        min_hits: int = 3,
    ) -> None:
        self.high_threshold = high_threshold
        self.low_threshold = low_threshold
        self.iou_threshold = iou_threshold
        self.reid_threshold = reid_threshold
        self.max_age = max_age
        self.min_hits = min_hits
        self.tracks: list[Track] = []
        self.frame_index = 0
        self._next_id = 1
        self.reid_recoveries = 0
        """How many identities were recovered by appearance that IoU alone would have lost."""

    def update(self, detections: Sequence[VisionResult]) -> list[Track]:
        """Advance one frame. Returns the tracks that are currently active."""
        self.frame_index += 1
        for track in self.tracks:
            track.predict()

        high = [d for d in detections if d.confidence >= self.high_threshold]
        low = [d for d in detections if self.low_threshold <= d.confidence < self.high_threshold]

        candidates = [t for t in self.tracks if t.state is not TrackState.REMOVED]

        # --- pass 1: confident detections against every live track --------------
        unmatched_tracks, unmatched_high = self._associate(candidates, high)

        # --- pass 2: the leftover tracks get the *weak* detections ---------------
        # This is ByteTrack's central idea. A partially occluded object produces a low-confidence
        # detection, and discarding it loses the identity — then a new id appears when the object
        # re-emerges, which is what turns one truck's visit into two.
        still_unmatched, _unmatched_low = self._associate(
            [candidates[index] for index in unmatched_tracks], low, appearance=False
        )
        lost_indices = {unmatched_tracks[index] for index in still_unmatched}

        for position, track in enumerate(candidates):
            if position in lost_indices:
                track.mark_lost()

        # --- new tracks from confident detections nobody claimed -----------------
        for index in unmatched_high:
            self._spawn(high[index])

        # --- retire tracks that have been gone too long -------------------------
        for track in self.tracks:
            if track.time_since_update > self.max_age:
                track.state = TrackState.REMOVED
        self.tracks = [t for t in self.tracks if t.state is not TrackState.REMOVED]

        return [t for t in self.tracks if t.state in (TrackState.CONFIRMED, TrackState.TENTATIVE)]

    def _associate(
        self,
        tracks: Sequence[Track],
        detections: Sequence[VisionResult],
        *,
        appearance: bool = True,
    ) -> tuple[list[int], list[int]]:
        """Match tracks to detections. Returns ``(unmatched_track_indices, unmatched_detections)``."""
        if not tracks or not detections:
            return list(range(len(tracks))), list(range(len(detections)))

        similarity = iou_matrix(tracks, detections)
        # Never associate across classes: a person box overlapping a truck box is not the truck.
        for row, track in enumerate(tracks):
            for column, detection in enumerate(detections):
                if track.label != detection.label:
                    similarity[row, column] = 0.0

        matches = greedy_assign(similarity, self.iou_threshold)

        if appearance:
            matched_tracks = {row for row, _ in matches}
            matched_detections = {column for _, column in matches}
            free_tracks = [index for index in range(len(tracks)) if index not in matched_tracks]
            free_detections = [
                index for index in range(len(detections)) if index not in matched_detections
            ]
            if free_tracks and free_detections:
                # Appearance rescue: an object that moved far enough for IoU to be zero (a gap in
                # detection, a fast turn) can still be recognised by how it looks.
                sub = cosine_matrix(
                    [tracks[index] for index in free_tracks],
                    [detections[index] for index in free_detections],
                )
                for row_index, column_index in greedy_assign(sub, self.reid_threshold):
                    track_index = free_tracks[row_index]
                    detection_index = free_detections[column_index]
                    if tracks[track_index].label != detections[detection_index].label:
                        continue
                    matches.append((track_index, detection_index))
                    self.reid_recoveries += 1

        for track_index, detection_index in matches:
            tracks[track_index].update(
                detections[detection_index], self.frame_index, min_hits=self.min_hits
            )

        matched_tracks = {row for row, _ in matches}
        matched_detections = {column for _, column in matches}
        return (
            [index for index in range(len(tracks)) if index not in matched_tracks],
            [index for index in range(len(detections)) if index not in matched_detections],
        )

    def _spawn(self, detection: VisionResult) -> Track:
        track = Track(
            track_id=self._next_id,
            label=detection.label,
            filter=KalmanBoxFilter(detection.bbox),
            confidence=detection.confidence,
            start_frame=self.frame_index,
            last_frame=self.frame_index,
            history=[detection.bbox],
            embedding=(
                np.asarray(detection.embedding, dtype=np.float32)
                if detection.embedding is not None
                else None
            ),
        )
        self._next_id += 1
        self.tracks.append(track)
        return track

    def stats(self) -> dict[str, Any]:
        return {
            "frame": self.frame_index,
            "tracks": len(self.tracks),
            "confirmed": sum(1 for t in self.tracks if t.state is TrackState.CONFIRMED),
            "lost": sum(1 for t in self.tracks if t.state is TrackState.LOST),
            "next_id": self._next_id,
            "reid_recoveries": self.reid_recoveries,
        }


def displacement(history: Sequence[BBox]) -> float:
    """Total path length of a track's centre, in pixels."""
    if len(history) < 2:
        return 0.0
    total = 0.0
    for previous, current in pairwise(history):
        px, py = previous.center
        cx, cy = current.center
        total += math.hypot(cx - px, cy - py)
    return total
