"""In-pipeline media redaction (PRD §14).

Faces and licence plates are blurred **before** a frame reaches the object store. That ordering is
the whole point: redacting on read means an un-redacted frame exists somewhere, and "we blur it when
you look at it" is not a privacy guarantee — it is a promise about every future code path that might
read that bucket.

What this is not: face *recognition*. There is no identity matching here and none is possible; the
face path exists solely to find pixels to destroy. Recognition stays off behind
``SIO_ENABLE_FACE_RECOGNITION`` (PRD NG2/R4).
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from sio_core import Settings, get_logger
from sio_core.ports import VisionResult
from sio_schemas import BBox

log = get_logger("sio.perception.redact")

# Where a face sits inside a person box: the top ~22% of the height, horizontally centred.
# Deliberately generous. A face detector would be more precise, but it is another model, another
# licence and another failure mode, and for redaction a region that is too large costs nothing while
# a region that is too small costs everything.
FACE_REGION = (0.24, 0.20, 0.60)  # (height fraction, x inset fraction, width fraction)

PLATE_CLASSES = frozenset({"car", "truck", "bus", "motorcycle", "vehicle"})
# A plate sits low and central on a vehicle's rear/front face.
PLATE_REGION = (0.62, 0.86, 0.28, 0.44)  # (y0, y1, x0 inset, width) as fractions


class Redactor:
    """Blurs privacy-sensitive regions of a frame."""

    def __init__(self, settings: Settings) -> None:
        self.blur_faces = settings.blur_faces
        self.blur_plates = settings.blur_plates
        self.retain_raw = settings.retain_raw
        if settings.enable_face_recognition:
            # Loud, because this flag has legal consequences (BIPA, CUBI, GDPR Art. 9) and someone
            # reading the logs after the fact should be able to see exactly when it was turned on.
            log.warning(
                "governance.face_recognition_enabled",
                note="face recognition is enabled; this requires legal review per docs/GOVERNANCE.md",
            )

    @property
    def enabled(self) -> bool:
        return self.blur_faces or self.blur_plates

    def apply(self, image: np.ndarray, results: list[VisionResult]) -> tuple[np.ndarray, int]:
        """Return ``(image, regions_blurred)``.

        The image is copied only when there is something to blur, so the common no-op path does not
        pay for a copy of every frame.
        """
        if not self.enabled or not results:
            return image, 0

        regions: list[BBox] = []
        for result in results:
            if self.blur_faces and result.label == "person":
                regions.append(self._face_region(result.bbox))
            if self.blur_plates and result.label in PLATE_CLASSES:
                regions.append(self._plate_region(result.bbox))

        if not regions:
            return image, 0

        output = image.copy()
        height, width = output.shape[:2]
        applied = 0
        for region in regions:
            clipped = region.clip(width, height)
            x1, y1 = int(clipped.x1), int(clipped.y1)
            x2, y2 = int(clipped.x2), int(clipped.y2)
            if x2 - x1 < 4 or y2 - y1 < 4:
                continue
            patch = output[y1:y2, x1:x2]
            # Kernel proportional to the region: a fixed kernel leaves a large face legible and
            # turns a small plate into a smear that still shows character positions.
            kernel = max(5, (min(x2 - x1, y2 - y1) // 2) | 1)
            output[y1:y2, x1:x2] = cv2.GaussianBlur(patch, (kernel, kernel), 0)
            # Pixelate on top of the blur: Gaussian blur alone can be partially inverted, and for a
            # small region that is a real risk rather than a theoretical one.
            small = cv2.resize(
                output[y1:y2, x1:x2],
                (max(1, (x2 - x1) // 8), max(1, (y2 - y1) // 8)),
                interpolation=cv2.INTER_LINEAR,
            )
            output[y1:y2, x1:x2] = cv2.resize(
                small, (x2 - x1, y2 - y1), interpolation=cv2.INTER_NEAREST
            )
            applied += 1
        return output, applied

    @staticmethod
    def _face_region(bbox: BBox) -> BBox:
        height_fraction, x_inset, width_fraction = FACE_REGION
        return BBox(
            x1=bbox.x1 + bbox.width * x_inset,
            y1=bbox.y1,
            x2=bbox.x1 + bbox.width * (x_inset + width_fraction),
            y2=bbox.y1 + bbox.height * height_fraction,
        )

    @staticmethod
    def _plate_region(bbox: BBox) -> BBox:
        y0, y1, x_inset, width_fraction = PLATE_REGION
        return BBox(
            x1=bbox.x1 + bbox.width * x_inset,
            y1=bbox.y1 + bbox.height * y0,
            x2=bbox.x1 + bbox.width * (x_inset + width_fraction),
            y2=bbox.y1 + bbox.height * y1,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "faces": self.blur_faces,
            "plates": self.blur_plates,
            "retain_raw": self.retain_raw,
            "method": "gaussian+pixelate",
        }
