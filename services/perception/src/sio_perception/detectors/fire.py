"""Fire and smoke detection — an honest stand-in, not a model.

There is no COCO class for fire, and no permissively-licensed ONNX fire detector we can rely on
being available. Rather than pretend, this is an explicit heuristic that combines the three signals
a fire actually produces on camera:

1. **colour** — fire occupies a narrow band of hue with high saturation and value;
2. **flicker** — a fire's bright region changes shape frame to frame, where a red truck does not.
   This is what separates the two, and a colour-only detector will happily report the truck;
3. **growth** — a real fire's area trends upward over seconds.

Every detection it produces is labelled with `heuristic: true` and carries its component scores in
`attrs`, so an explanation shows an operator *why* — and so nobody mistakes this for a trained
model. The upgrade path is a fine-tune of yolo26n on D-Fire or FASDD, exported to ONNX and dropped
in as SIO_DET_MODEL: no code change, because the `Detector` port is the same.
"""

from __future__ import annotations

from collections import deque
from typing import Any

import cv2
import numpy as np

from sio_core import get_logger
from sio_core.ports import VisionResult
from sio_schemas import BBox

log = get_logger("sio.perception.fire")

# Fire in HSV: hue from deep red through orange to yellow, high saturation, high value.
# OpenCV hue is 0-179, so this is roughly 0-40 degrees doubled.
FIRE_LOWER = np.array([0, 120, 180], dtype=np.uint8)
FIRE_UPPER = np.array([35, 255, 255], dtype=np.uint8)
# Smoke: desaturated mid-to-light grey. Deliberately narrow — the sky, a white wall and a concrete
# apron all live near here, which is why smoke requires motion before it is reported at all.
SMOKE_LOWER = np.array([0, 0, 90], dtype=np.uint8)
SMOKE_UPPER = np.array([180, 45, 210], dtype=np.uint8)

MIN_REGION_PX = 900
"""Below roughly 30x30 pixels this is a reflection, a brake light or a hi-vis vest."""


class FireHeuristicDetector:
    """Colour + flicker + growth fire/smoke detection, per camera.

    State is kept **per source**, because flicker is a property of a place over time. One shared
    history across eight cameras would compare last frame from Gate A with this frame from Dock 3
    and call the difference flicker.
    """

    name = "fire-heuristic"

    MIN_IRREGULARITY = 0.12
    """Shape-irregularity floor. A convex blob (vehicle, container, vest) scores near zero; a flame's
    ragged outline scores well above it. Translation-invariant, which is exactly what frame-difference
    flicker is not."""

    MIN_FLICKER = 0.15
    """Flicker floor below which nothing is reported, whatever the colour.

    Not a weight — a **gate**. A red truck, a hi-vis vest and a brake light all sit inside the fire
    colour band, and with colour merely weighted, a large enough red rectangle reaches the threshold
    on colour alone. (It did: my first version reported a stationary red truck as fire.) Physically
    this is the right test too — a fire whose bright region is not changing shape is not a fire.
    """

    def __init__(
        self,
        *,
        conf_threshold: float = 0.45,
        history: int = 6,
        enable_smoke: bool = True,
        min_region_px: int = MIN_REGION_PX,
        min_flicker: float | None = None,
        min_irregularity: float | None = None,
    ) -> None:
        self.conf_threshold = conf_threshold
        self.history_length = history
        self.enable_smoke = enable_smoke
        self.min_region_px = min_region_px
        self.min_flicker = self.MIN_FLICKER if min_flicker is None else min_flicker
        self.min_irregularity = (
            self.MIN_IRREGULARITY if min_irregularity is None else min_irregularity
        )
        self._history: dict[str, deque[np.ndarray]] = {}
        self._areas: dict[str, deque[float]] = {}
        self._suppressed = 0
        """Count of frames where colour matched but flicker did not — a useful diagnostic when
        someone asks why the red truck is not raising alarms."""

    # ------------------------------------------------------------------- public
    def detect(self, image: np.ndarray, *, source_id: str = "unknown") -> list[VisionResult]:
        if image is None or image.size == 0:
            return []
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        results: list[VisionResult] = []

        fire_mask = cv2.inRange(hsv, FIRE_LOWER, FIRE_UPPER)
        fire_mask = cv2.morphologyEx(fire_mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        flicker = self._flicker_score(source_id, fire_mask)
        growth = self._growth_score(source_id, float(fire_mask.sum()) / 255.0)

        regions = self._regions(fire_mask)
        if regions and flicker < self.min_flicker:
            # Colour matched, nothing moved. The stationary-red-object case.
            self._suppressed += 1
            log.debug(
                "fire.suppressed_no_flicker",
                source=source_id,
                flicker=round(flicker, 3),
                floor=self.min_flicker,
                regions=len(regions),
            )
            regions = []

        for box, area, irregularity in regions:
            if irregularity < self.min_irregularity:
                # Colour matched and something moved, but the shape is a rigid blob. This is the
                # *driving* red truck, and it is why frame-difference flicker alone is not enough:
                # a translating rectangle produces a large frame difference while its outline stays
                # a rectangle. Shape irregularity is translation-invariant, so it separates the two.
                self._suppressed += 1
                log.debug(
                    "fire.suppressed_rigid_shape",
                    source=source_id,
                    irregularity=round(irregularity, 3),
                    floor=self.min_irregularity,
                )
                continue
            colour_score = min(1.0, area / (image.shape[0] * image.shape[1] * 0.02))
            # No single term can carry a detection alone: colour maxes at 0.25, flicker at 0.35,
            # irregularity at 0.30 — a fire needs at least two of the three.
            confidence = 0.25 * colour_score + 0.35 * flicker + 0.30 * irregularity + 0.10 * growth
            if confidence < self.conf_threshold:
                continue
            results.append(
                VisionResult(
                    label="fire",
                    confidence=round(min(0.95, confidence), 3),
                    bbox=box,
                    attrs={
                        "heuristic": True,
                        "colour_score": round(colour_score, 3),
                        "flicker_score": round(flicker, 3),
                        "irregularity_score": irregularity,
                        "growth_score": round(growth, 3),
                        "area_px": int(area),
                        "method": "hsv+flicker+shape+growth",
                        "note": "heuristic stand-in for a fine-tuned fire model; see docs/MODELS.md",
                    },
                )
            )

        if self.enable_smoke and results:
            # Smoke is only reported alongside fire. On its own the colour band catches sky, concrete
            # and pale walls, and a detector that cries smoke at a cloudy afternoon is worse than no
            # smoke detection at all.
            smoke_mask = cv2.inRange(hsv, SMOKE_LOWER, SMOKE_UPPER)
            smoke_mask = cv2.morphologyEx(smoke_mask, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
            for box, area, _shape in self._regions(smoke_mask, min_px=self.min_region_px * 4):
                results.append(
                    VisionResult(
                        label="smoke",
                        confidence=round(min(0.8, 0.3 + 0.5 * flicker), 3),
                        bbox=box,
                        attrs={
                            "heuristic": True,
                            "area_px": int(area),
                            "method": "hsv+co-occurrence-with-fire",
                            "note": "smoke is only reported when fire is also present",
                        },
                    )
                )
        return results

    def warmup(self) -> None:
        self.detect(np.zeros((64, 64, 3), dtype=np.uint8), source_id="warmup")

    def close(self) -> None:
        self._history.clear()
        self._areas.clear()

    # ----------------------------------------------------------------- internals
    def _regions(
        self, mask: np.ndarray, *, min_px: int | None = None
    ) -> list[tuple[BBox, float, float]]:
        """Connected components above the size floor, largest first, each with a shape score.

        Returns ``(bbox, area_px, irregularity)``. Irregularity combines two cheap, translation- and
        scale-invariant measures of "does this outline look like a flame or like a box":

        * **1 - solidity** (region area over its convex hull area). A rectangle is ~1.0 solid; a
          flame with a ragged, concave boundary is much less.
        * **normalised perimeter complexity** (perimeter² / 4πarea, the inverse of circularity). A
          compact blob approaches 1; a wispy region is far above it.
        """
        floor = min_px or self.min_region_px
        count, labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
        regions: list[tuple[BBox, float, float]] = []
        for index in range(1, count):  # 0 is the background
            x, y, width, height, area = stats[index]
            if area < floor:
                continue
            component = (labels == index).astype(np.uint8)
            regions.append(
                (
                    BBox(x1=float(x), y1=float(y), x2=float(x + width), y2=float(y + height)),
                    float(area),
                    self._irregularity(component, float(area)),
                )
            )
        regions.sort(key=lambda item: item[1], reverse=True)
        return regions[:5]

    @staticmethod
    def _irregularity(component: np.ndarray, area: float) -> float:
        """Shape irregularity in [0, 1]: 0 for a convex blob, high for a ragged flame outline."""
        contours, _ = cv2.findContours(component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return 0.0
        contour = max(contours, key=cv2.contourArea)
        hull_area = cv2.contourArea(cv2.convexHull(contour))
        solidity = (area / hull_area) if hull_area > 0 else 1.0
        concavity = float(np.clip(1.0 - solidity, 0.0, 1.0))

        perimeter = cv2.arcLength(contour, True)
        # perimeter^2 / (4*pi*area) is 1 for a circle and grows with raggedness.
        complexity = (perimeter * perimeter) / (4.0 * np.pi * area) if area > 0 else 1.0
        raggedness = float(np.clip((complexity - 1.0) / 4.0, 0.0, 1.0))
        return round(float(np.clip(0.6 * concavity + 0.4 * raggedness, 0.0, 1.0)), 3)

    def _flicker_score(self, source_id: str, mask: np.ndarray) -> float:
        """How much the bright region changed since recent frames.

        A fire's mask boundary moves constantly; a parked red truck's does not. Scored as the mean
        absolute difference against the frames held for this source, normalised by the mask size.
        """
        small = cv2.resize(mask, (64, 64), interpolation=cv2.INTER_AREA)
        history = self._history.setdefault(source_id, deque(maxlen=self.history_length))
        if not history:
            history.append(small)
            return 0.0
        diffs = [
            float(np.abs(small.astype(np.int16) - past.astype(np.int16)).mean()) for past in history
        ]
        history.append(small)
        # ~12 grey levels of mean change is a vigorous flicker; scale to [0, 1] there.
        return float(min(1.0, (sum(diffs) / len(diffs)) / 12.0))

    def _growth_score(self, source_id: str, area: float) -> float:
        """Is the bright area trending upward? A developing fire grows; a brake light does not."""
        areas = self._areas.setdefault(source_id, deque(maxlen=self.history_length))
        if len(areas) < 2:
            areas.append(area)
            return 0.0
        earliest = areas[0]
        areas.append(area)
        if earliest <= 0:
            return 1.0 if area > 0 else 0.0
        return float(min(1.0, max(0.0, (area - earliest) / max(earliest, 1.0))))


class ThermalFireCorroborator:
    """Raises confidence in a visual fire detection when a nearby thermal sensor agrees.

    This is the cheap, honest half of "sensor fusion" for fire: a heuristic that a *camera* alone
    should not be trusted on becomes credible when the thermometer in the same zone is 40 degrees
    above its baseline. Kept separate from the detector because it needs IoT context, which is the
    fusion service's world rather than the vision engine's.
    """

    def __init__(self, *, baseline_c: float = 22.0, alarm_delta_c: float = 15.0) -> None:
        self.baseline_c = baseline_c
        self.alarm_delta_c = alarm_delta_c

    def corroboration(self, temperature_c: float | None) -> tuple[float, dict[str, Any]]:
        """Return a confidence multiplier and the evidence for it."""
        if temperature_c is None:
            return 1.0, {"thermal": "no reading"}
        delta = temperature_c - self.baseline_c
        if delta < self.alarm_delta_c:
            return 1.0, {"thermal_delta_c": round(delta, 1), "thermal": "normal"}
        # Cap the boost: corroboration should raise a maybe to a probably, not manufacture certainty.
        boost = min(1.6, 1.0 + (delta - self.alarm_delta_c) / 40.0)
        return boost, {
            "thermal_delta_c": round(delta, 1),
            "thermal": "elevated",
            "confidence_boost": round(boost, 2),
        }
