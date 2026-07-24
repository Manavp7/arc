"""Detectors that need no model weights.

``SyntheticDetector`` reads the ground truth the simulator attaches to each frame and emits it as
detections. That sounds like cheating, and it would be if it were the default — it is not. It exists
for three real jobs:

1. **CI.** The unit ring must run with no model files, and 186 MB of weights is not a test fixture.
2. **Pipeline work.** When the thing under test is fusion, or the events engine, or the timeline,
   spending 50 ms of CPU per frame re-deriving boxes that are already known is waste.
3. **Upper bound.** Running the pipeline with a perfect detector shows what the rest of the system
   would do with perfect perception, which is exactly the number you want when deciding whether a
   demo's weak point is the model or the logic downstream.

It is selected by ``SIO_DETECTOR=synthetic``, it announces itself in every detection's ``attrs``, and
``auto`` only falls back to it when the real weights are missing — with a warning.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from sio_core import get_logger
from sio_core.ports import VisionResult
from sio_schemas import BBox

log = get_logger("sio.perception.synthetic")


class SyntheticDetector:
    """Emits the simulator's ground truth as detections, with realistic imperfection.

    Perfect boxes would make the tracker's job trivially easy and hide association bugs, so boxes
    are jittered and detections are dropped at a configurable rate. The result exercises the same
    code paths a real detector does — including the ones that handle a missed detection.
    """

    name = "synthetic"

    def __init__(
        self,
        *,
        jitter_px: float = 3.0,
        drop_rate: float = 0.03,
        confidence: float = 0.9,
        seed: int = 1337,
    ) -> None:
        self.jitter_px = jitter_px
        self.drop_rate = drop_rate
        self.confidence = confidence
        self._rng = np.random.default_rng(seed)

    def detect(self, image: Any) -> list[VisionResult]:  # pragma: no cover - not the entry point
        """Not usable from pixels alone. Use :meth:`detect_from_payload`."""
        raise NotImplementedError(
            "SyntheticDetector reads the simulator's ground truth, not pixels; "
            "call detect_from_payload(observation.payload)"
        )

    def detect_from_payload(self, payload: dict[str, Any]) -> list[VisionResult]:
        """Convert a frame observation's ``visible`` ground truth into detections."""
        results: list[VisionResult] = []
        width = float(payload.get("width", 1280))
        height = float(payload.get("height", 720))

        for item in payload.get("visible", []):
            if self._rng.random() < self.drop_rate:
                continue  # a real detector misses things; so must this one
            box = item.get("bbox")
            if not box or len(box) != 4:
                continue
            jitter = self._rng.normal(0.0, self.jitter_px, 4)
            x1 = float(np.clip(box[0] + jitter[0], 0, width))
            y1 = float(np.clip(box[1] + jitter[1], 0, height))
            x2 = float(np.clip(box[2] + jitter[2], 0, width))
            y2 = float(np.clip(box[3] + jitter[3], 0, height))
            if x2 - x1 < 2 or y2 - y1 < 2:
                continue
            distance = float(item.get("distance_m", 20.0))
            # Confidence falls with distance, as a real detector's does.
            confidence = float(
                np.clip(self.confidence - distance / 400.0 + self._rng.normal(0, 0.02), 0.3, 0.99)
            )
            results.append(
                VisionResult(
                    label=str(item.get("class", "unknown")),
                    confidence=round(confidence, 3),
                    bbox=BBox(x1=x1, y1=y1, x2=x2, y2=y2),
                    attrs={
                        "synthetic": True,
                        "agent_id": item.get("agent_id"),
                        "distance_m": distance,
                        "ground_truth_label": item.get("label"),
                    },
                )
            )

        if payload.get("fire"):
            # The injected fire, as a detection a camera would plausibly make.
            results.append(
                VisionResult(
                    label="fire",
                    confidence=0.82,
                    bbox=BBox(x1=width * 0.42, y1=height * 0.45, x2=width * 0.58, y2=height * 0.72),
                    attrs={"synthetic": True, "injected": True},
                )
            )
        return results

    def warmup(self) -> None:
        return None

    def close(self) -> None:
        return None


class NullDetector:
    """Detects nothing.

    For running the pipeline with perception effectively switched off — measuring the cost of
    everything *else*, or reproducing a bug without 50 ms of inference in the way.
    """

    name = "null"

    def detect(self, image: Any) -> list[VisionResult]:
        return []

    def warmup(self) -> None:
        return None

    def close(self) -> None:
        return None


class DeepStreamDetector:
    """Placeholder for the NVIDIA DeepStream path (PRD §9.3, Phase 7).

    Raises with a specific, actionable message rather than silently behaving like something else.
    A stub that quietly returns no detections is worse than one that refuses to start: the pipeline
    would look healthy and find nothing.
    """

    name = "deepstream"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        from sio_core import ConfigError

        raise ConfigError(
            "the DeepStream detector lands in Phase 7. For GPU inference today, keep "
            "SIO_DETECTOR=onnx and set SIO_ORT_PROVIDERS=CUDAExecutionProvider — the same "
            ".onnx weights run on the GPU. See docs/GPU_SWAP.md."
        )

    def detect(self, image: Any) -> list[VisionResult]:  # pragma: no cover - unreachable
        return []

    def warmup(self) -> None:  # pragma: no cover
        return None

    def close(self) -> None:  # pragma: no cover
        return None
