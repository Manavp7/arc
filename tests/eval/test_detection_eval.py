"""Detection mAP (PRD §16, Phase 8).

Runs the **real** ONNX detector against **rendered frames with known boxes**, and scores it the way COCO does.

The fixture is the synthetic camera renderer: it composites sprites into a background at positions the simulator
chose, so the ground truth is exact rather than annotated. That has a limitation worth stating plainly, because
quoting this number without it would be misleading:

> **This measures how well the model detects sprite renderings, not how well it detects trucks.**

Real footage would give a number you could put in front of a customer; this gives a number that moves when the
pipeline breaks. Those are different jobs, and only the second one can run in CI on a laptop. What it catches is
real: a weights file that fails to load, a preprocessing change that shifts the input range, a letterbox
regression that offsets every box, an NMS threshold somebody tuned too far. Each of those makes mAP collapse,
and each has a habit of shipping otherwise, because a detector that returns *plausible* boxes looks fine.

mAP is implemented here rather than pulled from `pycocotools`, which is a C extension that needs a compiler on
macOS and pulls in the whole COCO API to score forty boxes. The implementation is ~50 lines and the parts that
are easy to get wrong — sorting by confidence globally rather than per image, counting a second match to the
same ground truth as a false positive, interpolating precision monotonically — are exactly the parts worth
having in front of us with the reasoning attached.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pytest

pytestmark = pytest.mark.eval

#: IoU thresholds, as COCO uses: 0.50 to 0.95 in steps of 0.05.
#:
#: The average over all ten is the headline. Reporting only mAP@0.50 flatters a model that localises loosely,
#: which matters here because a box that is off by a third still projects onto the wrong part of the yard.
IOU_THRESHOLDS = [round(0.5 + 0.05 * step, 2) for step in range(10)]

#: How many frames to score. Enough for a stable number, few enough that `just eval` finishes in seconds.
FRAMES = 12


@dataclass(frozen=True)
class Box:
    label: str
    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float = 1.0

    @property
    def area(self) -> float:
        return max(0.0, self.x2 - self.x1) * max(0.0, self.y2 - self.y1)


def iou(left: Box, right: Box) -> float:
    ix1, iy1 = max(left.x1, right.x1), max(left.y1, right.y1)
    ix2, iy2 = min(left.x2, right.x2), min(left.y2, right.y2)
    overlap = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = left.area + right.area - overlap
    return overlap / union if union > 0 else 0.0


def average_precision(
    predictions: list[tuple[int, Box]],
    ground_truth: dict[int, list[Box]],
    *,
    threshold: float,
) -> float:
    """AP for one class at one IoU threshold, by the COCO definition.

    Three details that are easy to get wrong and each of which inflates the score:

    * predictions are sorted by confidence **across all images**, not per image — sorting per image lets a weak
      detection in an easy frame outrank a strong one in a hard frame;
    * a ground-truth box can be matched **once**; a second detection of the same object is a false positive,
      which is what stops a model gaming the metric by predicting everything twice;
    * precision is made **monotonically decreasing** before integrating, which is what "interpolated" means and
      is the difference between AP and a sawtooth.
    """
    total_truth = sum(len(boxes) for boxes in ground_truth.values())
    if total_truth == 0:
        return math.nan

    ordered = sorted(predictions, key=lambda item: item[1].confidence, reverse=True)
    matched: dict[int, set[int]] = {frame: set() for frame in ground_truth}
    true_positives: list[int] = []
    false_positives: list[int] = []

    for frame, prediction in ordered:
        candidates = ground_truth.get(frame, [])
        best_iou, best_index = 0.0, -1
        for index, truth in enumerate(candidates):
            if index in matched.get(frame, set()):
                continue
            score = iou(prediction, truth)
            if score > best_iou:
                best_iou, best_index = score, index
        if best_iou >= threshold and best_index >= 0:
            matched.setdefault(frame, set()).add(best_index)
            true_positives.append(1)
            false_positives.append(0)
        else:
            true_positives.append(0)
            false_positives.append(1)

    if not ordered:
        return 0.0

    cumulative_tp = 0
    cumulative_fp = 0
    precisions: list[float] = []
    recalls: list[float] = []
    for tp, fp in zip(true_positives, false_positives, strict=True):
        cumulative_tp += tp
        cumulative_fp += fp
        precisions.append(cumulative_tp / (cumulative_tp + cumulative_fp))
        recalls.append(cumulative_tp / total_truth)

    # Monotonic envelope, right to left.
    for index in range(len(precisions) - 2, -1, -1):
        precisions[index] = max(precisions[index], precisions[index + 1])

    # 101-point interpolation, as COCO specifies. A plain trapezoid over the raw curve is close but not the
    # same number, and quoting "mAP" for a differently-computed quantity is how two teams end up disagreeing
    # about whether a model improved.
    total = 0.0
    for point in range(101):
        target = point / 100
        candidates = [
            precision
            for precision, recall in zip(precisions, recalls, strict=True)
            if recall >= target
        ]
        total += max(candidates) if candidates else 0.0
    return total / 101


def mean_average_precision(
    predictions: dict[str, list[tuple[int, Box]]],
    ground_truth: dict[str, dict[int, list[Box]]],
) -> tuple[float, dict[str, float]]:
    """mAP over classes and IoU thresholds, plus the per-class breakdown.

    The breakdown matters more than the headline in practice: a single number moving from 0.6 to 0.5 says
    something broke, and the per-class column says *what*.
    """
    per_class: dict[str, float] = {}
    for label, truth in ground_truth.items():
        scores = [
            average_precision(predictions.get(label, []), truth, threshold=threshold)
            for threshold in IOU_THRESHOLDS
        ]
        usable = [score for score in scores if not math.isnan(score)]
        per_class[label] = sum(usable) / len(usable) if usable else math.nan
    valid = [score for score in per_class.values() if not math.isnan(score)]
    return (sum(valid) / len(valid) if valid else math.nan), per_class


# --- the fixture ------------------------------------------------------------------------------------
def _render_fixture(frames: int) -> list[tuple[Any, list[Box]]]:
    """Rendered frames with exact ground truth.

    Deterministic by seed, so the number is comparable between runs. A fixture that regenerated randomly would
    make every mAP change ambiguous — model or fixture? — which is the fastest way to make a metric useless.
    """
    import numpy as np
    from sio_ingest.sim.renderer import CameraRenderer

    from sio_core import get_settings

    renderer = CameraRenderer(get_settings().samples_dir)
    if not renderer.load():
        return []

    # ONLY classes the renderer can actually draw, asked of the renderer rather than assumed.
    #
    # My first version hard-coded `["truck", "person", "forklift"]`. There is no forklift sprite, so `render`
    # returned None for any frame containing only forklifts and the class scored 0.00 — a zero that said
    # nothing about the detector and dragged the headline down by a third. Scoring a class the fixture cannot
    # draw is measuring the fixture, and reporting it as a model score is worse than not measuring at all.
    sprite_classes = [
        label for label, count in renderer.stats()["sprite_classes"].items() if count > 0
    ]
    if not sprite_classes:
        return []

    rng = np.random.default_rng(11)
    fixture: list[tuple[Any, list[Box]]] = []
    classes = sprite_classes

    for index in range(frames):
        visible: list[dict[str, Any]] = []
        truth: list[Box] = []
        for slot in range(int(rng.integers(2, 5))):
            label = classes[int(rng.integers(0, len(classes)))]
            width = (
                float(rng.integers(70, 150)) if label != "person" else float(rng.integers(30, 60))
            )
            height = width * (1.6 if label == "person" else 0.8)
            x1 = float(rng.integers(10, max(11, 620 - int(width))))
            y1 = float(rng.integers(10, max(11, 460 - int(height))))
            box = [x1, y1, x1 + width, y1 + height]
            visible.append({"bbox": box, "class": label, "agent_id": f"a{index}-{slot}"})
            truth.append(Box(label, *box))

        encoded = renderer.render(f"cam-eval-{index % 3}", {"visible": visible})
        if encoded is None:
            continue
        import cv2

        image = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
        fixture.append((image, truth))
    return fixture


def test_detection_map(scorecard) -> None:  # type: ignore[no-untyped-def]
    """Score the real detector on rendered frames.

    The floor is deliberately low. What this catches is collapse — weights that fail to load, preprocessing that
    shifts the input range, a letterbox regression that offsets every box — not a few points of drift. A tight
    floor on a synthetic fixture would fail on the day somebody improves the sprites.
    """
    floor = 0.15
    try:
        from sio_perception.factory import build_detector

        from sio_core import get_settings
    except ImportError as error:  # pragma: no cover - the perception extra is optional
        scorecard.skip(
            "detection mAP@[.5:.95]", floor=floor, reason=f"perception unavailable: {error}"
        )
        pytest.skip("perception is not installed")

    settings = get_settings()
    detector = build_detector(settings)
    if detector.name in ("synthetic", "null"):
        # Skipped rather than scored. A synthetic detector reading the boxes it was handed would report a
        # near-perfect mAP, and a scorecard line of 0.98 that means "we measured nothing" is worse than a gap.
        scorecard.skip(
            "detection mAP@[.5:.95]",
            floor=floor,
            reason=f"no real weights ({detector.name} detector); put yolo26n.onnx in .sio/models",
        )
        pytest.skip(f"detector is {detector.name}, not a real model")

    fixture = _render_fixture(FRAMES)
    if not fixture:
        scorecard.skip(
            "detection mAP@[.5:.95]",
            floor=floor,
            reason="the sprite renderer has no samples to draw",
        )
        pytest.skip("renderer unavailable")

    predictions: dict[str, list[tuple[int, Box]]] = {}
    ground_truth: dict[str, dict[int, list[Box]]] = {}

    for frame_index, (image, truth) in enumerate(fixture):
        for box in truth:
            ground_truth.setdefault(box.label, {}).setdefault(frame_index, []).append(box)
        for result in detector.detect(image):
            box = Box(
                result.label,
                float(result.bbox.x1),
                float(result.bbox.y1),
                float(result.bbox.x2),
                float(result.bbox.y2),
                float(result.confidence),
            )
            predictions.setdefault(result.label, []).append((frame_index, box))

    # Only classes the fixture contains. Scoring a class the fixture never draws would average in a zero and
    # say the detector got worse when the fixture simply did not exercise it.
    ground_truth = {label: frames for label, frames in ground_truth.items() if frames}
    score, per_class = mean_average_precision(predictions, ground_truth)

    breakdown = ", ".join(
        f"{label} {value:.2f}"
        for label, value in sorted(per_class.items())
        if not math.isnan(value)
    )
    recorded = scorecard.record(
        "detection mAP@[.5:.95]",
        0.0 if math.isnan(score) else score,
        floor=floor,
        detail=f"{len(fixture)} rendered frames, {detector.name} — {breakdown}",
    )
    assert recorded.passed, (
        f"mAP {recorded.value:.3f} is below the {floor} floor. That is a collapse rather than drift — "
        f"check that the weights load, that preprocessing still normalises to the range the model expects, "
        f"and that the letterbox offset is applied when boxes are mapped back."
    )


# --- the metric's own correctness -------------------------------------------------------------------
def test_a_perfect_detector_scores_one() -> None:
    """The metric has to be right before the number means anything."""
    truth = {"truck": {0: [Box("truck", 0, 0, 10, 10)]}}
    perfect = {"truck": [(0, Box("truck", 0, 0, 10, 10, 0.9))]}
    score, _ = mean_average_precision(perfect, truth)
    assert score == pytest.approx(1.0, abs=1e-6)


def test_a_detector_that_finds_nothing_scores_zero() -> None:
    truth = {"truck": {0: [Box("truck", 0, 0, 10, 10)]}}
    score, _ = mean_average_precision({}, truth)
    assert score == pytest.approx(0.0)


def test_a_duplicate_detection_is_counted_as_a_false_positive() -> None:
    """One ground-truth box matches once; the second detection is a false positive.

    A subtlety my first version of this test got wrong, and which is worth stating because it surprises people:
    a duplicate that arrives AFTER the correct detection does not reduce interpolated AP. Recall already reached
    1.0 at precision 1.0, and the monotonic envelope keeps it there. That is correct COCO behaviour, not a bug —
    interpolated AP is insensitive to trailing false positives.

    Where a duplicate does cost you is when it OUTRANKS a genuine detection of another object, which is the case
    below: the duplicate at 0.9 pushes the real detection of the second truck down the ranking, so precision at
    full recall is 2/3 rather than 1.
    """
    truth = {"truck": {0: [Box("truck", 0, 0, 10, 10), Box("truck", 50, 50, 60, 60)]}}
    doubled = {
        "truck": [
            (0, Box("truck", 0, 0, 10, 10, 0.95)),  # correct
            (0, Box("truck", 0, 0, 10, 10, 0.90)),  # the same object again: a false positive
            (0, Box("truck", 50, 50, 60, 60, 0.85)),  # the second truck, now ranked third
        ]
    }
    score, _ = mean_average_precision(doubled, truth)
    assert score < 1.0

    clean = {
        "truck": [
            (0, Box("truck", 0, 0, 10, 10, 0.95)),
            (0, Box("truck", 50, 50, 60, 60, 0.85)),
        ]
    }
    assert mean_average_precision(clean, truth)[0] == pytest.approx(1.0, abs=1e-6)


def test_a_loosely_localised_box_scores_lower_at_strict_thresholds() -> None:
    """Why the headline averages 0.50 to 0.95 rather than quoting mAP@0.50.

    A box that is slightly off passes at 0.50 and fails at 0.85 — and in this platform a loose box projects onto
    the wrong part of the yard, so the strict thresholds are the ones that matter operationally.

    The numbers here are chosen against the arithmetic rather than by eye: my first attempt used a 2px offset on
    a 10px box, assuming that was "slightly" off. It is IoU 0.47 — already failing at 0.50 — so the test failed
    and the metric was right. A 1px offset on a 20px box is IoU 0.82, which is the case I meant.
    """
    truth = {"truck": {0: [Box("truck", 0, 0, 20, 20)]}}
    loose = {"truck": [(0, Box("truck", 1, 1, 21, 21, 0.9))]}
    assert iou(Box("t", 1, 1, 21, 21), Box("t", 0, 0, 20, 20)) == pytest.approx(0.826, abs=0.01)
    assert average_precision(loose["truck"], truth["truck"], threshold=0.5) > 0.9
    assert average_precision(loose["truck"], truth["truck"], threshold=0.85) == pytest.approx(0.0)


def test_confidence_ordering_is_global_not_per_image() -> None:
    """Sorting per image lets a weak detection in an easy frame outrank a strong one in a hard frame.

    Here the correct box is low-confidence and the wrong one is high-confidence: a correct implementation ranks
    the wrong one first and pays for it.
    """
    truth = {"truck": {0: [Box("truck", 0, 0, 10, 10)], 1: [Box("truck", 0, 0, 10, 10)]}}
    predictions = {
        "truck": [
            (0, Box("truck", 50, 50, 60, 60, 0.99)),  # wrong, but confident
            (1, Box("truck", 0, 0, 10, 10, 0.10)),  # right, but timid
        ]
    }
    score, _ = mean_average_precision(predictions, truth)
    # Half the objects found, and the false positive ranked first, so precision at that recall is 0.5.
    assert 0.1 < score < 0.5


def test_iou_is_symmetric_and_bounded() -> None:
    left, right = Box("a", 0, 0, 10, 10), Box("a", 5, 5, 15, 15)
    assert iou(left, right) == pytest.approx(iou(right, left))
    assert 0.0 <= iou(left, right) <= 1.0
    assert iou(left, left) == pytest.approx(1.0)
    assert iou(left, Box("a", 100, 100, 110, 110)) == pytest.approx(0.0)
