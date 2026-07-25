"""Detector selection.

One place decides which detector runs, from configuration. Everything else in the service talks to
the :class:`~sio_core.ports.Detector` port, so swapping ONNX for DeepStream — or for the synthetic
detector in CI — changes nothing but an environment variable.

``SIO_DETECTOR=auto`` is the default and does the sensible thing: use the real weights if they are
present, otherwise fall back to synthetic **with a warning**, because a developer who has not run
``just models`` should get a working pipeline rather than a crash, and should also be told that the
detections they are looking at are not real.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sio_core import ConfigError, Settings, describe_error, get_logger
from sio_core.ports import Detector

from .detectors.fire import FireHeuristicDetector
from .detectors.onnx_yolo import OnnxReidEmbedder, OnnxYoloDetector, OnnxYoloSegDetector
from .detectors.synthetic import DeepStreamDetector, NullDetector, SyntheticDetector

log = get_logger("sio.perception.factory")


def _providers(settings: Settings) -> list[str]:
    """Execution providers, in preference order.

    This is the whole of the CPU→GPU swap for inference: same ``.onnx`` file, different provider.
    """
    configured = getattr(settings, "ort_providers", "") or ""
    requested = [name.strip() for name in configured.split(",") if name.strip()]
    return requested or ["CPUExecutionProvider"]


def models_present(settings: Settings) -> bool:
    return (Path(settings.model_dir) / settings.det_model).exists()


def build_detector(settings: Settings) -> Detector:
    """Construct the configured detector."""
    kind = settings.detector
    model_dir = Path(settings.model_dir)

    if kind == "auto":
        if models_present(settings):
            kind = "onnx_seg" if settings.enable_segmentation else "onnx"
        else:
            log.warning(
                "detector.auto_fallback",
                reason="model weights not found",
                looked_in=str(model_dir),
                using="synthetic",
                hint="run: just models",
            )
            kind = "synthetic"

    if kind == "synthetic":
        return SyntheticDetector(seed=settings.sim_seed)
    if kind == "null":
        return NullDetector()
    if kind == "deepstream":
        return DeepStreamDetector()
    if kind in ("onnx", "onnx_seg"):
        model_name = settings.seg_model if kind == "onnx_seg" else settings.det_model
        cls: Any = OnnxYoloSegDetector if kind == "onnx_seg" else OnnxYoloDetector
        return cls(
            model_dir / model_name,
            conf_threshold=settings.det_conf,
            imgsz=settings.det_imgsz,
            threads=settings.ort_threads,
            providers=_providers(settings),
        )
    raise ConfigError(f"unknown SIO_DETECTOR={kind!r}")


def build_fire_detector(settings: Settings) -> FireHeuristicDetector | None:
    """The fire heuristic runs *alongside* the main detector.

    Fire is not a COCO class, so it cannot come from the same forward pass — and the heuristic is
    cheap enough (a colour threshold and a frame difference) to run on every sampled frame.
    """
    return FireHeuristicDetector(conf_threshold=max(0.3, settings.det_conf))


def build_reid(settings: Settings) -> OnnxReidEmbedder | None:
    """The ReID embedder, when its weights are available.

    Optional by design: tracking degrades to IoU-only association without it, which still works —
    it just recovers fewer identities through occlusions.
    """
    path = Path(settings.model_dir) / settings.reid_model
    if not path.exists():
        log.warning(
            "reid.unavailable",
            looked_for=str(path),
            effect="tracking will use IoU association only",
            hint="run: just models",
        )
        return None
    try:
        return OnnxReidEmbedder(path, threads=settings.ort_threads, providers=_providers(settings))
    except Exception as exc:
        log.error("reid.load_failed", error=describe_error(exc))
        return None
