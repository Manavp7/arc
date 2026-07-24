"""Tracking quality metrics: HOTA, and the pieces it decomposes into.

The PRD asks for "stable track ids with a HOTA-oriented evaluation harness" (M4). HOTA is the right
metric because it separates the two ways tracking fails, which MOTA conflates:

* **DetA** — detection accuracy. Did we find the objects at all?
* **AssA** — association accuracy. Having found them, did we keep the same id on the same object?

A tracker that finds everything and shuffles ids scores well on MOTA and is useless for SIO: dwell
time, "which camera last saw X" and journey history all depend on the *id* being stable, not on the
box being present. HOTA = sqrt(DetA · AssA) refuses to hide that.

Implemented directly rather than depending on TrackEval: the full library is a large dependency with
its own data-format expectations, and what is needed here is one number per sequence over ground truth
the simulator already produces.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sio_schemas import BBox


@dataclass(frozen=True)
class GroundTruthBox:
    frame: int
    object_id: str
    bbox: BBox
    label: str = "unknown"


@dataclass(frozen=True)
class PredictedBox:
    frame: int
    track_id: int
    bbox: BBox
    label: str = "unknown"


@dataclass
class TrackingMetrics:
    """One sequence's scores."""

    hota: float
    det_a: float
    ass_a: float
    true_positives: int
    false_positives: int
    false_negatives: int
    id_switches: int
    fragmentations: int
    gt_objects: int
    predicted_tracks: int
    mostly_tracked: int
    """Ground-truth objects tracked for at least 80% of their life — the number an operator feels."""
    mostly_lost: int
    localisation: float
    """Mean IoU over matched pairs."""

    def summary(self) -> dict[str, Any]:
        return {
            "HOTA": round(self.hota, 4),
            "DetA": round(self.det_a, 4),
            "AssA": round(self.ass_a, 4),
            "TP": self.true_positives,
            "FP": self.false_positives,
            "FN": self.false_negatives,
            "IDSW": self.id_switches,
            "Frag": self.fragmentations,
            "MT": self.mostly_tracked,
            "ML": self.mostly_lost,
            "LocA": round(self.localisation, 4),
            "gt_objects": self.gt_objects,
            "pred_tracks": self.predicted_tracks,
        }

    def __str__(self) -> str:
        return (
            f"HOTA {self.hota:.3f} (DetA {self.det_a:.3f}, AssA {self.ass_a:.3f})  "
            f"TP {self.true_positives} FP {self.false_positives} FN {self.false_negatives}  "
            f"IDSW {self.id_switches}  MT {self.mostly_tracked}/{self.gt_objects}"
        )


@dataclass
class _Matcher:
    """Per-frame greedy matching plus the bookkeeping HOTA's association term needs."""

    iou_threshold: float = 0.5
    matches: list[tuple[str, int, float]] = field(default_factory=list)
    """(ground-truth id, track id, IoU) for every matched pair, across all frames."""

    def match_frame(
        self, truth: list[GroundTruthBox], predictions: list[PredictedBox]
    ) -> tuple[int, int, int]:
        """Match one frame. Returns ``(true_positives, false_positives, false_negatives)``."""
        pairs: list[tuple[float, int, int]] = []
        for gt_index, gt in enumerate(truth):
            for pred_index, prediction in enumerate(predictions):
                if gt.label != prediction.label:
                    continue
                iou = gt.bbox.iou(prediction.bbox)
                if iou >= self.iou_threshold:
                    pairs.append((iou, gt_index, pred_index))

        pairs.sort(reverse=True)
        used_gt: set[int] = set()
        used_pred: set[int] = set()
        true_positives = 0
        for iou, gt_index, pred_index in pairs:
            if gt_index in used_gt or pred_index in used_pred:
                continue
            used_gt.add(gt_index)
            used_pred.add(pred_index)
            self.matches.append((truth[gt_index].object_id, predictions[pred_index].track_id, iou))
            true_positives += 1

        return true_positives, len(predictions) - len(used_pred), len(truth) - len(used_gt)


def evaluate_tracking(
    ground_truth: list[GroundTruthBox],
    predictions: list[PredictedBox],
    *,
    iou_threshold: float = 0.5,
) -> TrackingMetrics:
    """Score a tracked sequence against ground truth."""
    frames = sorted({box.frame for box in ground_truth} | {box.frame for box in predictions})
    truth_by_frame: dict[int, list[GroundTruthBox]] = defaultdict(list)
    predictions_by_frame: dict[int, list[PredictedBox]] = defaultdict(list)
    for box in ground_truth:
        truth_by_frame[box.frame].append(box)
    for box in predictions:
        predictions_by_frame[box.frame].append(box)

    matcher = _Matcher(iou_threshold=iou_threshold)
    true_positives = false_positives = false_negatives = 0
    for frame in frames:
        tp, fp, fn = matcher.match_frame(truth_by_frame[frame], predictions_by_frame[frame])
        true_positives += tp
        false_positives += fp
        false_negatives += fn

    # --- DetA: the usual detection Jaccard index -----------------------------
    denominator = true_positives + false_positives + false_negatives
    det_a = true_positives / denominator if denominator else 0.0

    # --- association ---------------------------------------------------------
    # For each matched pair, HOTA asks: of all the times this ground-truth object and this track
    # appeared, how often were they matched to *each other*? That is what punishes id shuffling
    # while a plain box-overlap metric does not notice it.
    gt_counts: dict[str, int] = defaultdict(int)
    track_counts: dict[int, int] = defaultdict(int)
    pair_counts: dict[tuple[str, int], int] = defaultdict(int)
    for gt_id, track_id, _iou in matcher.matches:
        gt_counts[gt_id] += 1
        track_counts[track_id] += 1
        pair_counts[(gt_id, track_id)] += 1

    association_sum = 0.0
    for (gt_id, track_id), count in pair_counts.items():
        union = gt_counts[gt_id] + track_counts[track_id] - count
        association_sum += (count / union) * count if union else 0.0
    ass_a = association_sum / true_positives if true_positives else 0.0

    hota = (det_a * ass_a) ** 0.5

    # --- id switches and fragmentations --------------------------------------
    per_object: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for frame in frames:
        frame_matcher = _Matcher(iou_threshold=iou_threshold)
        frame_matcher.match_frame(truth_by_frame[frame], predictions_by_frame[frame])
        for gt_id, track_id, _iou in frame_matcher.matches:
            per_object[gt_id].append((frame, track_id))

    id_switches = 0
    fragmentations = 0
    mostly_tracked = 0
    mostly_lost = 0
    gt_frames: dict[str, int] = defaultdict(int)
    for box in ground_truth:
        gt_frames[box.object_id] += 1

    for gt_id, appearances in per_object.items():
        appearances.sort()
        previous_track: int | None = None
        previous_frame: int | None = None
        for frame, track_id in appearances:
            if previous_track is not None and track_id != previous_track:
                id_switches += 1
            if previous_frame is not None and frame != previous_frame + 1:
                fragmentations += 1
            previous_track, previous_frame = track_id, frame
        coverage = len(appearances) / max(1, gt_frames[gt_id])
        if coverage >= 0.8:
            mostly_tracked += 1
        elif coverage <= 0.2:
            mostly_lost += 1

    # Ground-truth objects never matched at all are mostly lost.
    mostly_lost += len(set(gt_frames) - set(per_object))

    localisation = (
        sum(iou for _gt, _track, iou in matcher.matches) / len(matcher.matches)
        if matcher.matches
        else 0.0
    )

    return TrackingMetrics(
        hota=hota,
        det_a=det_a,
        ass_a=ass_a,
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        id_switches=id_switches,
        fragmentations=fragmentations,
        gt_objects=len(gt_frames),
        predicted_tracks=len({box.track_id for box in predictions}),
        mostly_tracked=mostly_tracked,
        mostly_lost=mostly_lost,
        localisation=localisation,
    )


def synthetic_sequence(
    *,
    objects: int = 3,
    frames: int = 60,
    occlusion: tuple[int, int] | None = (20, 26),
    detection_noise: float = 2.0,
    drop_rate: float = 0.0,
    manoeuvre_during_occlusion: bool = False,
    seed: int = 7,
) -> tuple[list[GroundTruthBox], list[Any]]:
    """A deterministic MOT sequence with known ground truth.

    Returns ``(ground_truth, per_frame_detections)``. The occlusion window is the point: it is where a
    tracker either holds an identity or invents a new one, and that difference is invisible in a
    detection metric but obvious in AssA.

    ``manoeuvre_during_occlusion`` is the harder case, and the one that actually exercises appearance
    matching. A constant-velocity Kalman filter coasts through a straight-line occlusion perfectly
    well — so an object that vanishes and reappears where predicted is recovered by geometry alone,
    and ReID never gets asked. Make the object *turn* while hidden and the prediction lands somewhere
    else entirely: IoU is then zero on re-emergence, and appearance is the only cue left.
    """
    import numpy as np

    from sio_core.ports import VisionResult

    rng = np.random.default_rng(seed)
    truth: list[GroundTruthBox] = []
    detections_per_frame: list[list[VisionResult]] = []

    lanes = [120 + index * 150 for index in range(objects)]
    for frame in range(frames):
        frame_detections: list[VisionResult] = []
        for index, lane_y in enumerate(lanes):
            # Constant-velocity motion across the frame.
            x = 80 + frame * 14 + index * 30
            y = float(lane_y)
            if manoeuvre_during_occlusion and index == 0 and occlusion and frame > occlusion[0]:
                # Object 0 turns hard while hidden, so the filter's prediction is wrong when it
                # reappears and only appearance can recover the identity.
                elapsed = frame - occlusion[0]
                x = 80 + occlusion[0] * 14 + index * 30 + elapsed * 4
                y = float(lane_y) + elapsed * 16
            box = BBox(x1=float(x), y1=y, x2=float(x + 90), y2=y + 120)
            truth.append(
                GroundTruthBox(frame=frame, object_id=f"obj-{index}", bbox=box, label="truck")
            )

            hidden = occlusion is not None and occlusion[0] <= frame <= occlusion[1] and index == 0
            if hidden or rng.random() < drop_rate:
                continue  # occluded or missed: no detection this frame

            jitter = rng.normal(0, detection_noise, 4)
            frame_detections.append(
                VisionResult(
                    label="truck",
                    confidence=float(np.clip(0.85 + rng.normal(0, 0.05), 0.2, 0.99)),
                    bbox=BBox(
                        x1=max(0.0, box.x1 + jitter[0]),
                        y1=max(0.0, box.y1 + jitter[1]),
                        x2=box.x2 + jitter[2],
                        y2=box.y2 + jitter[3],
                    ),
                    embedding=tuple(_stable_embedding(index)),
                )
            )
        detections_per_frame.append(frame_detections)

    return truth, detections_per_frame


def _stable_embedding(index: int, dim: int = 32) -> list[float]:
    """A deterministic unit vector per object, so appearance matching has something to match."""
    import numpy as np

    rng = np.random.default_rng(1000 + index)
    vector = rng.normal(0, 1, dim)
    return (vector / np.linalg.norm(vector)).tolist()
