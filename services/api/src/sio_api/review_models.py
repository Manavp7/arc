"""Server-configured local detectors and immutable recorded-analysis profiles."""

from __future__ import annotations

import hashlib
import importlib.util
import math
import tempfile
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_MODEL_BYTES = 256 * 1024 * 1024
_INSPECTION_LOCK = threading.Lock()
MOTION_NAME = "OpenCV foreground change + geometric region association"
MOTION_WARNING = "Motion regions are changes from the first frame, not classified people or vehicles. Fixed-camera clips only; camera movement and lighting changes can cause false events."


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["auto", "motion", "onnx"] = "auto"
    confidence_threshold: float | None = Field(
        default=None, ge=0.05, le=0.95, allow_inf_nan=False, strict=True
    )
    sample_fps: int = Field(default=2, ge=1, le=2, strict=True)


class AnalysisProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    requested_mode: Literal["auto", "motion", "onnx"]
    resolved_mode: Literal["motion", "onnx"]
    confidence_threshold: float = Field(ge=0.05, le=0.95, allow_inf_nan=False, strict=True)
    sample_fps: int = Field(ge=1, le=2, strict=True)
    model_id: Literal["motion", "configured_onnx"]
    model_name: str
    weights_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    imgsz: int = Field(ge=128, le=1280, strict=True)
    warning: str | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.resolved_mode == "onnx" and (
            self.model_id != "configured_onnx" or not self.weights_sha256
        ):
            raise ValueError("An ONNX profile requires its pinned configured artifact")
        if self.resolved_mode == "motion" and (self.model_id != "motion" or self.weights_sha256):
            raise ValueError("Motion profiles cannot claim an ONNX artifact")
        if self.requested_mode != "auto" and self.requested_mode != self.resolved_mode:
            raise ValueError("Explicit model choice cannot change modes")
        return self


def default_threshold(settings: Any) -> float:
    value = float(settings.det_conf)
    return value if math.isfinite(value) and 0.05 <= value <= 0.95 else 0.35


def configured_weights(settings: Any) -> Path:
    # Only administrator configuration supplies this path. Requests never accept model paths.
    return Path(settings.model_dir) / settings.det_model


def artifact_hash(path: Path) -> str:
    try:
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_MODEL_BYTES:
            raise ValueError(
                "Configured ONNX detector is missing or exceeds the 256 MiB local limit"
            )
        digest, total = hashlib.sha256(), 0
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_MODEL_BYTES:
                    raise ValueError("Configured ONNX detector exceeds the 256 MiB local limit")
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as error:
        raise ValueError("Configured ONNX detector could not be read") from error


def load_pinned_detector(settings: Any, profile: dict[str, Any]):
    from sio_perception.detectors.onnx_yolo import OnnxYoloDetector

    pinned = AnalysisProfile.model_validate(profile)
    if pinned.resolved_mode != "onnx":
        raise ValueError("An ONNX profile is required")
    path = configured_weights(settings)
    # Loading an isolated copy pins the exact bytes consumed by ONNX Runtime and prevents
    # untracked external-data sidecars being picked up beside the configured model.
    with tempfile.TemporaryDirectory(prefix="sio-review-model-") as temporary:
        isolated = Path(temporary) / "detector.onnx"
        digest, total = hashlib.sha256(), 0
        try:
            with path.open("rb") as source, isolated.open("wb") as destination:
                while chunk := source.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_MODEL_BYTES:
                        raise ValueError("Configured ONNX detector exceeds the 256 MiB local limit")
                    digest.update(chunk)
                    destination.write(chunk)
        except OSError as error:
            raise ValueError(
                "The pinned ONNX artifact is unavailable; restore it or start a new analysis"
            ) from error
        if digest.hexdigest() != pinned.weights_sha256:
            raise ValueError(
                "Configured ONNX weights changed after enqueue; restore the pinned artifact or start a new analysis"
            )
        detector = None
        try:
            detector = OnnxYoloDetector(
                isolated,
                conf_threshold=pinned.confidence_threshold,
                imgsz=pinned.imgsz,
                threads=1,
                max_detections=32,
            )
            detector.warmup()
            detector.name = pinned.model_name
            return detector
        except Exception as error:
            if detector is not None:
                detector.close()
            raise ValueError(
                "The pinned ONNX detector could not load as a compatible local detector; no motion fallback was used"
            ) from error


@lru_cache(maxsize=4)
def inspect_detector(path: str, digest: str, imgsz: int) -> dict[str, Any]:
    from types import SimpleNamespace

    profile = AnalysisProfile(
        requested_mode="onnx",
        resolved_mode="onnx",
        model_id="configured_onnx",
        model_name=Path(path).name,
        weights_sha256=digest,
        imgsz=imgsz,
        confidence_threshold=0.35,
        sample_fps=2,
    )
    try:
        detector = load_pinned_detector(
            SimpleNamespace(model_dir=Path(path).parent, det_model=Path(path).name),
            profile.model_dump(),
        )
        try:
            return {"available": True, "classes": sorted(set(detector.names.values()))[:200]}
        finally:
            detector.close()
    except Exception:
        return {
            "available": False,
            "unavailable_reason": "Configured ONNX model is not a loadable compatible detector",
        }


def model_catalog(settings: Any) -> dict[str, Any]:
    decoder = importlib.util.find_spec("cv2") is not None
    motion = {"mode": "motion", "model_id": "motion", "name": MOTION_NAME, "available": decoder}
    onnx: dict[str, Any] = {
        "mode": "onnx",
        "model_id": "configured_onnx",
        "name": Path(settings.det_model).name,
        "available": False,
    }
    if not decoder or importlib.util.find_spec("onnxruntime") is None:
        onnx["unavailable_reason"] = "OpenCV and ONNX Runtime must be installed locally"
    else:
        try:
            digest = artifact_hash(configured_weights(settings))
            with _INSPECTION_LOCK:
                inspection = inspect_detector(
                    str(configured_weights(settings)), digest, int(settings.det_imgsz)
                )
            onnx.update(weights_sha256=digest, **inspection)
        except ValueError as error:
            onnx["unavailable_reason"] = str(error)
    return {
        "models": [motion, onnx],
        "sample_fps": [1, 2],
        "confidence_threshold": {"min": 0.05, "max": 0.95, "default": default_threshold(settings)},
        "default_mode": "auto",
    }


def pin_profile(settings: Any, request: AnalysisRequest) -> dict[str, Any]:
    threshold = (
        request.confidence_threshold
        if request.confidence_threshold is not None
        else default_threshold(settings)
    )
    selected: dict[str, Any] = {"available": False}
    if request.mode != "motion":
        selected = model_catalog(settings)["models"][1]
    if request.mode == "onnx" and not selected["available"]:
        raise ValueError(
            selected.get("unavailable_reason", "Configured ONNX detector is unavailable")
        )
    use_onnx = request.mode != "motion" and selected["available"]
    return AnalysisProfile(
        requested_mode=request.mode,
        resolved_mode="onnx" if use_onnx else "motion",
        model_id="configured_onnx" if use_onnx else "motion",
        model_name=selected["name"] if use_onnx else MOTION_NAME,
        weights_sha256=selected.get("weights_sha256") if use_onnx else None,
        confidence_threshold=threshold,
        sample_fps=request.sample_fps,
        imgsz=int(settings.det_imgsz),
        warning=selected.get("unavailable_reason")
        if request.mode == "auto" and not use_onnx
        else None,
    ).model_dump(mode="json")


def validate_artifact(settings: Any, profile: dict[str, Any]) -> dict[str, Any]:
    pinned = AnalysisProfile.model_validate(profile)
    if (
        pinned.resolved_mode == "onnx"
        and artifact_hash(configured_weights(settings)) != pinned.weights_sha256
    ):
        raise ValueError(
            "Configured ONNX weights changed after enqueue; restore the pinned artifact or start a new analysis"
        )
    return pinned.model_dump(mode="json")
