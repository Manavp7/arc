"""Anomaly reporting: the numbers an operator is shown must be numbers they can believe.

The detector's *detections* were fine. What was not fine was one of the sentences it produced, and this file
exists because of it:

    severe_events_per_min was 2, baseline 0 (+1997141.7 robust sigma)

A figure like that does more damage than a missed detection. It is so obviously wrong that it invites the
reader to distrust every other number on the panel, including all the correct ones.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sio_events.anomaly import FeatureVector, RobustZScoreDetector


# --- the two-million-sigma bug ------------------------------------------------------------------
def test_a_zero_baseline_does_not_produce_an_absurd_sigma() -> None:
    """The alert that started this: `severe_events_per_min was 2, baseline 0 (+1997141.7 robust sigma)`.

    A count feature that has been zero all window has a MAD of zero, and the old floor scaled with the
    magnitude — a sensible idea that degenerates completely at zero, collapsing the denominator to 1e-6.

    "Two events where there are normally none" is a genuine anomaly. It is not measurable in sigmas, because
    a window with no variation supplies no sigma. And 1997141.7 does active harm: it is so obviously wrong
    that it invites the reader to distrust every other figure on the panel, including the correct ones.
    """
    from sio_events.anomaly import MAX_REPORTABLE_Z, _robust_z

    z = _robust_z(2.0, 0.0, 0.0)
    assert abs(z) <= MAX_REPORTABLE_Z, f"a zero baseline produced {z}"
    assert abs(z) >= 4.0, "it must still be well past any threshold — this is a real anomaly"


def test_the_cap_does_not_stop_anything_flagging() -> None:
    """The cap is on the reported number, not on the detection."""
    detector = RobustZScoreDetector(warmup=5, window=50, z_threshold=4.0)
    start = datetime.now(UTC)
    for index in range(30):
        detector.observe(
            FeatureVector(ts=start + timedelta(seconds=index), values={"severe_per_min": 0.0})
        )
    verdict = detector.observe(
        FeatureVector(ts=start + timedelta(seconds=31), values={"severe_per_min": 2.0})
    )
    assert verdict.is_anomaly, "two events where there are normally none must still flag"


def test_a_degenerate_baseline_is_described_in_words_not_sigmas() -> None:
    """Quoting a sigma that does not exist is how the absurd figure reached a screen."""
    detector = RobustZScoreDetector(warmup=5, window=50)
    start = datetime.now(UTC)
    for index in range(30):
        detector.observe(
            FeatureVector(ts=start + timedelta(seconds=index), values={"severe_per_min": 0.0})
        )
    verdict = detector.observe(
        FeatureVector(ts=start + timedelta(seconds=31), values={"severe_per_min": 2.0})
    )
    reason = verdict.reasons[0]
    assert "where there is normally none" in reason
    assert "sigma" not in reason, f"still quoting a sigma for a window with no variation: {reason}"


def test_an_ordinary_deviation_still_reports_its_sigma() -> None:
    """The fix must not flatten the normal case into vague language."""
    from sio_events.anomaly import _robust_z

    z = _robust_z(25.0, 20.0, 1.0)
    assert 3.0 < z < 4.0, f"an ordinary 5-unit deviation should be a few sigma, got {z}"


def test_a_tiny_change_on_a_flat_series_is_not_an_incident() -> None:
    """A sensor reporting exactly 20.0 for an hour and then 20.1 is not an incident.

    The original floor existed for this, and the fix must keep it working.
    """
    from sio_events.anomaly import _robust_z

    assert abs(_robust_z(20.1, 20.0, 0.0)) < 1.0
