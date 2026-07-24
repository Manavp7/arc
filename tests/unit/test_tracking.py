"""Tests for the tracking engine.

The question tracking has to answer is not "did you find the object" but "did you keep the same id on
it". So these tests are mostly about identity across adversity: occlusion, a manoeuvre while hidden,
two objects crossing, and low-confidence detections that a naive tracker throws away.
"""

from __future__ import annotations

from itertools import pairwise

import numpy as np
import pytest
from sio_tracking.bytetrack import (
    ByteTracker,
    KalmanBoxFilter,
    TrackState,
    displacement,
    greedy_assign,
)
from sio_tracking.crosscam import CrossCameraAssociator
from sio_tracking.evaluate import (
    GroundTruthBox,
    PredictedBox,
    evaluate_tracking,
    synthetic_sequence,
)

from sio_core.ports import VisionResult
from sio_schemas import BBox


def detection(
    x: float,
    y: float,
    *,
    width: float = 90,
    height: float = 120,
    conf: float = 0.9,
    label: str = "truck",
    embedding: tuple[float, ...] | None = None,
) -> VisionResult:
    return VisionResult(
        label=label,
        confidence=conf,
        bbox=BBox(x1=x, y1=y, x2=x + width, y2=y + height),
        embedding=embedding,
    )


def unit_vector(seed: int, dim: int = 32) -> tuple[float, ...]:
    rng = np.random.default_rng(seed)
    vector = rng.normal(0, 1, dim)
    return tuple((vector / np.linalg.norm(vector)).tolist())


# ------------------------------------------------------------------ kalman filter
def test_kalman_filter_tracks_constant_velocity() -> None:
    """The filter must be able to coast: that is what carries a track through an occlusion."""
    box = BBox(x1=100, y1=100, x2=190, y2=220)
    kalman = KalmanBoxFilter(box)
    for step in range(1, 11):
        kalman.predict()
        kalman.update(BBox(x1=100 + step * 10, y1=100, x2=190 + step * 10, y2=220))

    velocity_x, _velocity_y = kalman.velocity
    assert velocity_x == pytest.approx(10.0, abs=2.0), "should have learned the horizontal speed"

    # Now predict without any measurement, as during an occlusion.
    before = kalman.box.center[0]
    kalman.predict()
    after = kalman.box.center[0]
    assert after > before, "an unmeasured step must still advance the estimate"


def test_kalman_filter_models_size_separately_from_motion() -> None:
    """A receding object moves *and* shrinks; the two must not fight each other."""
    kalman = KalmanBoxFilter(BBox(x1=100, y1=100, x2=200, y2=300))
    for step in range(1, 9):
        kalman.predict()
        shrink = step * 8
        kalman.update(
            BBox(x1=100 + step * 5, y1=100 + shrink // 2, x2=200 + step * 5, y2=300 - shrink // 2)
        )
    box = kalman.box
    assert box.height < 200, "height should have followed the shrinking object"
    assert box.center[0] > 150, "and the centre should still have moved right"


# ----------------------------------------------------------------- assignment
def test_greedy_assignment_is_one_to_one_and_best_first() -> None:
    cost = np.array([[0.9, 0.2], [0.8, 0.7]], dtype=np.float32)
    assert greedy_assign(cost, 0.3) == [(0, 0), (1, 1)]


def test_greedy_assignment_respects_the_threshold() -> None:
    cost = np.array([[0.2, 0.1]], dtype=np.float32)
    assert greedy_assign(cost, 0.3) == []


def test_greedy_assignment_handles_an_empty_matrix() -> None:
    assert greedy_assign(np.zeros((0, 0), dtype=np.float32), 0.3) == []


# --------------------------------------------------------------------- tracking
def test_a_track_is_confirmed_after_min_hits() -> None:
    """An unconfirmed track must not reach the world model: one spurious detection would create an
    entity, and a world model full of one-frame ghosts is worse than one a few frames behind."""
    tracker = ByteTracker(min_hits=3)
    for step in range(2):
        active = tracker.update([detection(100 + step * 12, 100)])
        assert all(track.state is TrackState.TENTATIVE for track in active)
    active = tracker.update([detection(124, 100)])
    assert any(track.state is TrackState.CONFIRMED for track in active)


def test_identity_is_stable_across_a_simple_sequence() -> None:
    tracker = ByteTracker(min_hits=2)
    ids: set[int] = set()
    for step in range(20):
        for track in tracker.update([detection(50 + step * 15, 200)]):
            ids.add(track.track_id)
    assert ids == {1}, f"one object should be one id, got {ids}"


def test_two_objects_get_two_ids_and_keep_them_when_crossing() -> None:
    """Crossing paths is the classic id-swap trap."""
    tracker = ByteTracker(min_hits=2)
    seen: dict[int, list[float]] = {}
    for step in range(24):
        left = detection(40 + step * 18, 150)  # moving right
        right = detection(460 - step * 18, 152)  # moving left, nearly the same row
        for track in tracker.update([left, right]):
            seen.setdefault(track.track_id, []).append(track.box.center[0])
    assert len(seen) == 2, f"expected exactly two identities, got {len(seen)}"
    # Each identity should have moved monotonically in one direction, not swapped.
    for positions in seen.values():
        deltas = [b - a for a, b in pairwise(positions)]
        forward = sum(1 for delta in deltas if delta > 0)
        assert forward in (0, len(deltas)) or abs(forward - len(deltas) / 2) > len(deltas) * 0.3, (
            "an identity appears to have swapped direction mid-sequence"
        )


def test_low_confidence_detections_keep_a_track_alive() -> None:
    """ByteTrack's central idea. A weak detection from a partial occlusion must not be discarded."""
    tracker = ByteTracker(min_hits=2, high_threshold=0.5, low_threshold=0.1)
    for step in range(4):
        tracker.update([detection(60 + step * 14, 180, conf=0.9)])
    confirmed = [t for t in tracker.tracks if t.state is TrackState.CONFIRMED]
    assert confirmed
    original_id = confirmed[0].track_id

    # Now only weak detections arrive, as during an occlusion.
    for step in range(4, 9):
        tracker.update([detection(60 + step * 14, 180, conf=0.25)])

    ids = {t.track_id for t in tracker.tracks}
    assert ids == {original_id}, f"the identity should survive on weak detections, got {ids}"
    assert tracker.stats()["next_id"] - 1 == 1, "no new identity should have been invented"


def test_a_track_is_retired_after_max_age() -> None:
    tracker = ByteTracker(min_hits=2, max_age=5)
    for step in range(3):
        tracker.update([detection(100 + step * 10, 100)])
    for _ in range(8):
        tracker.update([])
    assert tracker.tracks == [], "a track with no detections must eventually be retired"


def test_class_mismatches_are_never_associated() -> None:
    """A person box overlapping a truck box is not the truck."""
    tracker = ByteTracker(min_hits=1)
    tracker.update([detection(100, 100, label="truck")])
    tracker.update([detection(102, 101, label="person")])
    labels = {track.label for track in tracker.tracks}
    assert labels == {"truck", "person"}, "the classes must be tracked separately"
    assert tracker.stats()["next_id"] - 1 == 2


def test_appearance_is_smoothed_not_replaced() -> None:
    """One bad crop must not poison a track's identity signature."""
    tracker = ByteTracker(min_hits=1)
    good = unit_vector(1)
    tracker.update([detection(100, 100, embedding=good)])
    track = tracker.tracks[0]
    first = track.embedding.copy()  # type: ignore[union-attr]

    tracker.update([detection(112, 100, embedding=unit_vector(999))])  # a wildly different crop
    after = track.embedding
    assert after is not None
    similarity = float(np.dot(first, after))
    assert similarity > 0.95, "a single outlier crop should barely move the signature"


# ------------------------------------------------------------------------- HOTA
def test_perfect_tracking_scores_one() -> None:
    truth = [
        GroundTruthBox(
            frame=frame, object_id="a", bbox=BBox(x1=10, y1=10, x2=50, y2=90), label="truck"
        )
        for frame in range(10)
    ]
    predictions = [
        PredictedBox(frame=frame, track_id=1, bbox=BBox(x1=10, y1=10, x2=50, y2=90), label="truck")
        for frame in range(10)
    ]
    metrics = evaluate_tracking(truth, predictions)
    assert metrics.hota == pytest.approx(1.0)
    assert metrics.id_switches == 0
    assert metrics.mostly_tracked == 1


def test_id_shuffling_is_punished_by_assa_not_deta() -> None:
    """The whole reason for using HOTA rather than MOTA."""
    box = BBox(x1=10, y1=10, x2=50, y2=90)
    truth = [
        GroundTruthBox(frame=frame, object_id="a", bbox=box, label="truck") for frame in range(10)
    ]
    # Every box is found, but the id changes every frame.
    shuffled = [
        PredictedBox(frame=frame, track_id=frame, bbox=box, label="truck") for frame in range(10)
    ]
    metrics = evaluate_tracking(truth, shuffled)
    assert metrics.det_a == pytest.approx(1.0), "detection is perfect"
    assert metrics.ass_a < 0.2, "association should be terrible"
    assert metrics.hota < 0.5, "HOTA must reflect the association failure"
    assert metrics.id_switches == 9


def test_missed_objects_lower_deta() -> None:
    box = BBox(x1=10, y1=10, x2=50, y2=90)
    truth = [
        GroundTruthBox(frame=frame, object_id="a", bbox=box, label="truck") for frame in range(10)
    ]
    half = [PredictedBox(frame=frame, track_id=1, bbox=box, label="truck") for frame in range(5)]
    metrics = evaluate_tracking(truth, half)
    assert metrics.false_negatives == 5
    assert metrics.det_a == pytest.approx(0.5)
    assert metrics.ass_a == pytest.approx(1.0), "the ids that exist are perfectly consistent"


def test_evaluation_handles_empty_input() -> None:
    assert evaluate_tracking([], []).hota == 0.0


# ------------------------------------------------------- end-to-end on a sequence
def test_tracker_holds_identity_through_a_straight_occlusion() -> None:
    truth, per_frame = synthetic_sequence(objects=3, frames=60, occlusion=(20, 26), seed=7)
    tracker = ByteTracker(min_hits=3, max_age=30)
    predictions: list[PredictedBox] = []
    for frame_index, detections in enumerate(per_frame):
        for track in tracker.update(detections):
            predictions.append(
                PredictedBox(
                    frame=frame_index, track_id=track.track_id, bbox=track.box, label=track.label
                )
            )
    metrics = evaluate_tracking(truth, predictions)
    assert metrics.hota > 0.9, f"HOTA {metrics.hota:.3f} is too low"
    assert metrics.id_switches == 0
    assert tracker.stats()["next_id"] - 1 == 3, "three objects should produce three identities"


def test_appearance_rescues_identity_when_prediction_fails() -> None:
    """The case that justifies ReID at all.

    A constant-velocity filter coasts through a straight-line occlusion, so appearance is never
    needed and looks useless. Make the object manoeuvre while hidden and the prediction lands
    elsewhere: IoU is zero on re-emergence and appearance is the only remaining cue.
    """

    def run(reid_threshold: float) -> tuple[float, int]:
        truth, per_frame = synthetic_sequence(
            objects=3, frames=70, occlusion=(20, 30), manoeuvre_during_occlusion=True, seed=7
        )
        tracker = ByteTracker(min_hits=3, max_age=30, reid_threshold=reid_threshold)
        predictions: list[PredictedBox] = []
        for frame_index, detections in enumerate(per_frame):
            for track in tracker.update(detections):
                predictions.append(
                    PredictedBox(
                        frame=frame_index,
                        track_id=track.track_id,
                        bbox=track.box,
                        label=track.label,
                    )
                )
        metrics = evaluate_tracking(truth, predictions)
        return metrics.ass_a, tracker.stats()["next_id"] - 1

    with_appearance, ids_with = run(0.75)
    without_appearance, ids_without = run(2.0)  # unreachable threshold disables the rescue

    assert with_appearance > without_appearance, (
        f"appearance rescue should improve association ({with_appearance:.3f} vs {without_appearance:.3f})"
    )
    assert ids_with == 3, "three objects, three identities"
    assert ids_without > ids_with, "without appearance an identity should be lost and reinvented"


def test_displacement_measures_path_length() -> None:
    history = [BBox(x1=0, y1=0, x2=10, y2=10), BBox(x1=10, y1=0, x2=20, y2=10)]
    assert displacement(history) == pytest.approx(10.0)
    assert displacement(history[:1]) == 0.0


# ------------------------------------------------------------------ cross-camera
def make_internal(track_id: int, label: str, embedding: tuple[float, ...], hits: int = 10):
    from sio_tracking.bytetrack import Track as InternalTrack

    return InternalTrack(
        track_id=track_id,
        label=label,
        filter=KalmanBoxFilter(BBox(x1=0, y1=0, x2=10, y2=10)),
        confidence=0.9,
        hits=hits,
        embedding=np.asarray(embedding, dtype=np.float32),
    )


def envelope(track_id: str, label: str = "truck"):
    from sio_schemas import Track as TrackEnvelope

    return TrackEnvelope(track_id=track_id, **{"class": label}, source_id="cam-x")


def test_cross_camera_links_the_same_appearance_on_different_cameras() -> None:
    associator = CrossCameraAssociator(reid_threshold=0.7)
    associator.MIN_TRANSIT_S = 0.0  # the test does not want to wait
    vector = unit_vector(5)

    assert associator.observe("cam-a", make_internal(1, "truck", vector), envelope("t1")) == []
    matches = associator.observe("cam-b", make_internal(1, "truck", vector), envelope("t2"))
    assert matches == ["t1"]
    assert associator.link_count == 1


def test_cross_camera_ignores_the_same_camera() -> None:
    """Within one camera, identity is ByteTrack's job."""
    associator = CrossCameraAssociator(reid_threshold=0.7)
    associator.MIN_TRANSIT_S = 0.0
    vector = unit_vector(5)
    associator.observe("cam-a", make_internal(1, "truck", vector), envelope("t1"))
    assert associator.observe("cam-a", make_internal(2, "truck", vector), envelope("t2")) == []


def test_cross_camera_requires_the_same_class() -> None:
    associator = CrossCameraAssociator(reid_threshold=0.5)
    associator.MIN_TRANSIT_S = 0.0
    vector = unit_vector(5)
    associator.observe("cam-a", make_internal(1, "truck", vector), envelope("t1"))
    assert (
        associator.observe("cam-b", make_internal(1, "person", vector), envelope("t2", "person"))
        == []
    )


def test_cross_camera_requires_a_settled_embedding() -> None:
    """An embedding smoothed over one frame is dominated by whichever crop came first."""
    associator = CrossCameraAssociator(reid_threshold=0.5)
    associator.MIN_TRANSIT_S = 0.0
    vector = unit_vector(5)
    associator.observe("cam-a", make_internal(1, "truck", vector, hits=10), envelope("t1"))
    assert (
        associator.observe("cam-b", make_internal(1, "truck", vector, hits=2), envelope("t2")) == []
    )


def test_cross_camera_rejects_dissimilar_appearance() -> None:
    associator = CrossCameraAssociator(reid_threshold=0.8)
    associator.MIN_TRANSIT_S = 0.0
    associator.observe("cam-a", make_internal(1, "truck", unit_vector(1)), envelope("t1"))
    assert (
        associator.observe("cam-b", make_internal(1, "truck", unit_vector(2)), envelope("t2")) == []
    )
    assert associator.rejected_similarity >= 1


def test_cross_camera_can_be_disabled() -> None:
    associator = CrossCameraAssociator(enabled=False)
    vector = unit_vector(5)
    associator.observe("cam-a", make_internal(1, "truck", vector), envelope("t1"))
    assert associator.observe("cam-b", make_internal(1, "truck", vector), envelope("t2")) == []


def test_cross_camera_describes_itself_honestly() -> None:
    description = CrossCameraAssociator().describe()
    assert "hypotheses" in description["note"], "the output must not read as an assertion of fact"
