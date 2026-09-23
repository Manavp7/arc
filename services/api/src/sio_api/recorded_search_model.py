"""Pinned local CLIP runtime for recorded search; no model downloads or hash fallback."""

from __future__ import annotations

import hashlib
import importlib.util
import math
import tempfile
from functools import lru_cache
from pathlib import Path

from sio_core.vision.clip_embedder import OnnxClipEmbedder

# Same reviewed int8 exports as infra/models.json. Requests cannot choose artifacts.
ASSETS = (
    ("clip_vision_model", "0ab0c1b3ace708e539633af1744d5a95247fe4e14d3e08ff197ef82a6cb9bd93"),
    ("clip_text_model", "18845f2ccc35223bb7fec403383a131154b11ac0918df25cf51986df5efd3a21"),
    ("clip_tokenizer", "f7f3b7af117d467b58374797691a6438d3e6b9e9cef800dfd5dced7f697a90cd"),
)
MODEL_ID = (
    "clip-vit-base-patch32-int8-"
    + hashlib.sha256(":".join(digest for _, digest in ASSETS).encode()).hexdigest()[:16]
)
MAX_ASSET_BYTES = 128 * 1024 * 1024


def file_hash(path: Path, limit: int) -> str:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise ValueError("Required local artifact is missing or exceeds its size limit")
    digest, count = hashlib.sha256(), 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            count += len(chunk)
            if count > limit:
                raise ValueError("Local artifact exceeds its size limit")
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=12)
def _checked_hash(path: str, signature: tuple[int, ...]) -> str:
    return file_hash(Path(path), MAX_ASSET_BYTES)


def model_paths(settings):
    root = Path(settings.model_dir).resolve()
    paths = [root / getattr(settings, key) for key, _ in ASSETS]
    if any(path.is_symlink() or not path.resolve().is_relative_to(root) for path in paths):
        raise ValueError("CLIP assets must be regular files inside the configured model directory")
    return paths


def check_model(settings):
    try:
        if any(
            importlib.util.find_spec(name) is None for name in ("onnxruntime", "tokenizers", "cv2")
        ):
            raise ValueError(
                "Install the project's locked Python dependencies to enable local CLIP"
            )
        for path, (_, expected) in zip(model_paths(settings), ASSETS, strict=True):
            stat = path.stat()
            signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            if _checked_hash(str(path), signature) != expected:
                raise ValueError(
                    "Configured CLIP assets do not match the pinned recorded-search model"
                )
        return {"available": True, "model_id": MODEL_ID, "reason": None}
    except (OSError, ValueError):
        return {
            "available": False,
            "model_id": MODEL_ID,
            "reason": "Install the three pinned CLIP assets and locked Python dependencies; see docs/RECORDED_SEARCH.md. No substitute embeddings are used.",
        }


def load_model(settings):
    if not check_model(settings)["available"]:
        raise ValueError("The pinned CLIP model is unavailable")
    # Load isolated copies and hash the consumed bytes. An adjacent ONNX external-data
    # file cannot silently change the embedding space without changing the pinned model.
    with tempfile.TemporaryDirectory(prefix="sio-recorded-clip-") as temporary:
        copies = []
        for index, (source, (_, expected)) in enumerate(
            zip(model_paths(settings), ASSETS, strict=True)
        ):
            target = Path(temporary) / (f"model-{index}.onnx" if index < 2 else "tokenizer.json")
            digest, count = hashlib.sha256(), 0
            with source.open("rb") as reader, target.open("xb") as writer:
                while chunk := reader.read(1024 * 1024):
                    count += len(chunk)
                    if count > MAX_ASSET_BYTES:
                        raise ValueError("CLIP asset exceeds its size limit")
                    writer.write(chunk)
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                raise ValueError("CLIP assets changed while loading; restore the pinned model")
            copies.append(target)
        model = OnnxClipEmbedder(*copies, threads=1, providers=["CPUExecutionProvider"])
        if model.dim != 512:
            model.close()
            raise ValueError("The pinned model must return 512-dimensional vectors")
        return model


def vector(value):
    if not isinstance(value, list) or len(value) != 512:
        raise ValueError("Invalid CLIP vector shape")
    if any(not isinstance(x, (float, int)) or not math.isfinite(x) for x in value):
        raise ValueError("Invalid CLIP vector values")
    norm = math.sqrt(sum(x * x for x in value))
    if not 0.999 <= norm <= 1.001:
        raise ValueError("Invalid CLIP vector norm")
    return value


class FrameReader:
    def __init__(self, path):
        import cv2

        self.capture = cv2.VideoCapture(str(path))
        if not self.capture.isOpened():
            self.capture.release()
            raise ValueError("Recording could not be decoded for indexing")

    def sample(self, at_s):
        import cv2

        self.capture.set(cv2.CAP_PROP_POS_MSEC, at_s * 1000)
        ok, image = self.capture.read()
        if (
            not ok
            or image is None
            or not 16 <= image.shape[0] <= 1080
            or not 16 <= image.shape[1] <= 1920
        ):
            raise ValueError("A retained recording sample could not be decoded within its bounds")
        return image

    def close(self):
        self.capture.release()
