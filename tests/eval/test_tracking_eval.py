"""Tracking HOTA on the synthetic MOT sequence (PRD §16, Phase 8).

HOTA rather than MOTA, and the reason is the whole point of measuring tracking separately from detection.

MOTA is dominated by detection errors: a tracker that never holds an identity but sits on top of a good detector
scores well, because false positives and false negatives swamp the identity-switch term. HOTA splits the two —
`DetA` for what was found, `AssA` for whether it stayed the same thing — so a regression in association is
visible instead of being absorbed. That distinction is operational here: an entity that changes id every few
frames breaks dwell time, breaks zone-entry events, and breaks every question of the form "how long has that
been there".

The sequence is `synthetic_sequence`, which exists in the tracking service because the tracker's own unit tests
need it. Its occlusion window is the interesting part: it is where a tracker either holds an identity or invents
a new one, and that difference is invisible in a detection metric.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.eval


def _run_tracker(detections_per_frame: list) -> list:  # type: ignore[type-arg]
    """Drive the real tracker over a sequence, returning predicted boxes.

    The REAL tracker, not a stand-in. An eval that scored a mock would measure the harness — and the thing worth
    catching here is a change to ByteTrack's thresholds or the Kalman filter's process noise, neither of which a
    mock has.
    """
    from sio_tracking.bytetrack import ByteTracker
    from sio_tracking.evaluate import PredictedBox

    # The same parameters the tracker's own unit tests use, so a difference in the eval score is a difference
    # in the tracker rather than in how it was configured here.
    tracker = ByteTracker(min_hits=3, max_age=30)
    predictions: list[PredictedBox] = []
    for frame_index, detections in enumerate(detections_per_frame):
        for track in tracker.update(detections):
            predictions.append(
                PredictedBox(
                    frame=frame_index,
                    track_id=track.track_id,
                    bbox=track.box,
                    label=track.label,
                )
            )
    return predictions


def test_tracking_hota(scorecard) -> None:  # type: ignore[no-untyped-def]
    """HOTA on a clean sequence with one occlusion.

    The floor is 0.50. Below that the tracker is not holding identities through the occlusion at all, which is
    the specific failure this sequence exists to catch — and which no detection metric would show.
    """
    floor = 0.50
    try:
        from sio_tracking.evaluate import evaluate_tracking, synthetic_sequence
    except ImportError as error:  # pragma: no cover
        scorecard.skip("tracking HOTA", floor=floor, reason=f"tracking unavailable: {error}")
        pytest.skip("tracking is not installed")

    truth, detections = synthetic_sequence(objects=3, frames=60, occlusion=(20, 26), seed=7)
    metrics = evaluate_tracking(truth, _run_tracker(detections))

    recorded = scorecard.record(
        "tracking HOTA",
        metrics.hota,
        floor=floor,
        detail=(
            f"DetA {metrics.det_a:.2f} AssA {metrics.ass_a:.2f} "
            f"IDSW {metrics.id_switches} MT {metrics.mostly_tracked}/{metrics.gt_objects}"
        ),
    )
    # Recorded separately, because the two halves fail for completely different reasons and a combined number
    # sends whoever reads it to the wrong place. DetA falling means the detector or the association radius;
    # AssA falling means the Kalman filter, the appearance model, or the occlusion handling.
    scorecard.record(
        "tracking AssA (identity)",
        metrics.ass_a,
        floor=0.40,
        detail=f"{metrics.id_switches} identity switches over {metrics.gt_objects} objects",
    )

    assert recorded.passed, (
        f"HOTA {metrics.hota:.3f} is below the {floor} floor. Check AssA first: if it has fallen and DetA has "
        f"not, the tracker is finding objects and losing their identities through the occlusion — look at the "
        f"Kalman process noise and the appearance threshold rather than at the detector."
    )


def test_tracking_holds_identity_through_a_manoeuvre(scorecard) -> None:  # type: ignore[no-untyped-def]
    """The harder sequence: the object changes direction while hidden.

    A constant-velocity Kalman filter coasts through a straight-line occlusion perfectly, so the easy sequence
    does not actually exercise appearance matching — it rewards a tracker that simply extrapolates. This one
    manoeuvres during the occlusion, so extrapolation puts the prediction in the wrong place and only appearance
    recovers the identity.

    Scored with a lower floor on purpose. It is genuinely hard, and a floor set at the easy sequence's level
    would fail on a tracker that is behaving reasonably.
    """
    floor = 0.30
    try:
        from sio_tracking.evaluate import evaluate_tracking, synthetic_sequence
    except ImportError as error:  # pragma: no cover
        scorecard.skip(
            "tracking HOTA (manoeuvre)", floor=floor, reason=f"tracking unavailable: {error}"
        )
        pytest.skip("tracking is not installed")

    truth, detections = synthetic_sequence(
        objects=3, frames=60, occlusion=(20, 26), manoeuvre_during_occlusion=True, seed=7
    )
    metrics = evaluate_tracking(truth, _run_tracker(detections))

    recorded = scorecard.record(
        "tracking HOTA (manoeuvre)",
        metrics.hota,
        floor=floor,
        detail=(
            f"DetA {metrics.det_a:.2f} AssA {metrics.ass_a:.2f} IDSW {metrics.id_switches} "
            f"— direction change during occlusion"
        ),
    )
    assert recorded.passed, (
        f"HOTA {metrics.hota:.3f} on the manoeuvre sequence is below {floor}. This is the sequence that "
        f"exercises appearance matching rather than extrapolation."
    )


def test_a_dropped_detection_rate_degrades_gracefully(scorecard) -> None:  # type: ignore[no-untyped-def]
    """What happens when the detector misses 20% of frames.

    Worth measuring because it is the realistic condition, not the exceptional one: a real detector on a real
    camera misses frames constantly — motion blur, partial occlusion, an awkward angle. A tracker that only
    works on complete detections is one that works in the lab.

    Recorded rather than gated: the interesting output is the SHAPE of the degradation, and the scorecard's job
    is to show that dropping a fifth of detections costs a little rather than everything.
    """
    floor = 0.30
    try:
        from sio_tracking.evaluate import evaluate_tracking, synthetic_sequence
    except ImportError as error:  # pragma: no cover
        scorecard.skip(
            "tracking HOTA (20% drops)", floor=floor, reason=f"tracking unavailable: {error}"
        )
        pytest.skip("tracking is not installed")

    truth, detections = synthetic_sequence(objects=3, frames=60, drop_rate=0.2, seed=7)
    metrics = evaluate_tracking(truth, _run_tracker(detections))

    recorded = scorecard.record(
        "tracking HOTA (20% drops)",
        metrics.hota,
        floor=floor,
        detail=f"DetA {metrics.det_a:.2f} AssA {metrics.ass_a:.2f} — a fifth of detections missing",
    )
    assert recorded.passed, (
        f"HOTA {metrics.hota:.3f} with 20% dropped detections is below {floor}. A tracker that collapses at "
        f"this drop rate will not survive a real camera."
    )
