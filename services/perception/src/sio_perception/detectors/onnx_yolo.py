"""YOLO26 detection and segmentation on ONNX Runtime.

No PyTorch. Ultralytics publishes pre-exported ONNX weights, so this is `onnxruntime` plus
`opencv` — which means the GPU path is an execution-provider string rather than a different
dependency tree (see ``docs/GPU_SWAP.md``).

**The decode is trivial, and that is the point.** YOLO26's default head is end-to-end: it emits
``[1, 300, 6]`` where each row is ``x1, y1, x2, y2, confidence, class``, already sorted by
descending confidence and already de-duplicated. There is no NMS to implement, no anchor grid to
decode, no DFL bins to integrate. Post-processing is: threshold, map the class index through the
names embedded in the model file, and invert the letterbox.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from sio_core import ModelUnavailable, get_logger
from sio_core.ports import VisionResult
from sio_schemas import BBox

log = get_logger("sio.perception.onnx")


class Letterbox:
    """Resize-with-padding, and the inverse mapping back to source pixels.

    Kept as an object rather than a pair of functions because the *inverse* is the part that matters:
    a detection is only useful in source-image coordinates (for cropping a ReID patch, for drawing a
    box, for projecting to the ground plane), and getting the inversion subtly wrong produces boxes
    that look plausible and are consistently a few percent off.
    """

    def __init__(self, source_shape: tuple[int, int], size: int) -> None:
        source_height, source_width = source_shape
        self.scale = min(size / source_height, size / source_width)
        self.new_width = round(source_width * self.scale)
        self.new_height = round(source_height * self.scale)
        # Centre the image in the square canvas; halves, not the whole pad, or every box is offset.
        self.pad_x = (size - self.new_width) / 2
        self.pad_y = (size - self.new_height) / 2
        self.size = size
        self.source_width = source_width
        self.source_height = source_height

    def apply(self, image: np.ndarray) -> np.ndarray:
        resized = cv2.resize(
            image, (self.new_width, self.new_height), interpolation=cv2.INTER_LINEAR
        )
        canvas = np.full((self.size, self.size, 3), 114, dtype=np.uint8)  # 114 = Ultralytics' grey
        top, left = round(self.pad_y), round(self.pad_x)
        canvas[top : top + self.new_height, left : left + self.new_width] = resized
        return canvas

    def invert(
        self, x1: float, y1: float, x2: float, y2: float
    ) -> tuple[float, float, float, float]:
        """Map a box from letterboxed coordinates back to source pixels, clipped to the image."""
        inv = 1.0 / self.scale
        return (
            float(np.clip((x1 - self.pad_x) * inv, 0, self.source_width)),
            float(np.clip((y1 - self.pad_y) * inv, 0, self.source_height)),
            float(np.clip((x2 - self.pad_x) * inv, 0, self.source_width)),
            float(np.clip((y2 - self.pad_y) * inv, 0, self.source_height)),
        )


def _load_session(path: Path, *, threads: int, providers: list[str] | None = None) -> Any:
    try:
        import onnxruntime as ort
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise ModelUnavailable("onnxruntime is not installed; run: just setup") from exc

    if not path.exists():
        raise ModelUnavailable(f"model file not found: {path}\nrun: just models")

    options = ort.SessionOptions()
    # Bounded threads on purpose: a dozen services on one laptop each grabbing every core makes
    # everything slower. SIO_ORT_THREADS is the knob.
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    chosen = providers or ["CPUExecutionProvider"]
    available = set(ort.get_available_providers())
    usable = [provider for provider in chosen if provider in available] or ["CPUExecutionProvider"]
    if usable != chosen:
        log.warning("onnx.provider_fallback", requested=chosen, using=usable)
    return ort.InferenceSession(str(path), sess_options=options, providers=usable)


def _class_names(session: Any) -> dict[int, str]:
    """Read the class names Ultralytics embeds in the model file.

    Shipping a separate labels file alongside a model is a classic source of silent mislabelling:
    the two drift and suddenly every truck is a bus. The names travel inside the ONNX metadata, so
    they cannot disagree with the weights.
    """
    metadata = session.get_modelmeta().custom_metadata_map or {}
    raw = metadata.get("names")
    if not raw:
        log.warning("onnx.no_class_names", hint="falling back to numeric labels")
        return {}
    try:
        # Ultralytics writes a python-style dict literal, e.g. "{0: 'person', 1: 'bicycle'}".
        return {int(k): str(v) for k, v in json.loads(raw.replace("'", '"')).items()}
    except (ValueError, AttributeError):
        import ast

        try:
            return {int(k): str(v) for k, v in ast.literal_eval(raw).items()}
        except (ValueError, SyntaxError):
            log.warning("onnx.unparseable_class_names", raw=str(raw)[:80])
            return {}


class OnnxYoloDetector:
    """YOLO26 object detection.

    Threading note: an ``InferenceSession`` is safe to call concurrently, but the perception service
    runs inference in a worker thread anyway so a 25 ms CPU inference does not block the event loop
    that is also consuming the bus.
    """

    def __init__(
        self,
        model_path: Path | str,
        *,
        conf_threshold: float = 0.35,
        imgsz: int = 640,
        threads: int = 2,
        providers: list[str] | None = None,
        max_detections: int = 300,
    ) -> None:
        self.model_path = Path(model_path)
        self.name = self.model_path.name
        self.conf_threshold = conf_threshold
        self.imgsz = imgsz
        self.max_detections = max_detections
        self.session = _load_session(self.model_path, threads=threads, providers=providers)
        self.input_name = self.session.get_inputs()[0].name
        self.names = _class_names(self.session)
        output_shapes = [output.shape for output in self.session.get_outputs()]
        log.info(
            "detector.loaded",
            model=self.name,
            classes=len(self.names),
            outputs=output_shapes,
            providers=self.session.get_providers(),
        )

    # ---------------------------------------------------------------- inference
    def _preprocess(self, image: np.ndarray) -> tuple[np.ndarray, Letterbox]:
        letterbox = Letterbox(image.shape[:2], self.imgsz)
        canvas = letterbox.apply(image)
        # BGR (OpenCV) to RGB, HWC to CHW, 0-255 to 0-1.
        tensor = canvas[:, :, ::-1].transpose(2, 0, 1).astype(np.float32) / 255.0
        return np.ascontiguousarray(tensor)[None], letterbox

    def detect(self, image: np.ndarray) -> list[VisionResult]:
        if image is None or image.size == 0:
            return []
        tensor, letterbox = self._preprocess(image)
        outputs = self.session.run(None, {self.input_name: tensor})
        return self._postprocess(outputs, letterbox)

    def _postprocess(self, outputs: list[np.ndarray], letterbox: Letterbox) -> list[VisionResult]:
        """Decode the end-to-end head: ``[1, 300, 6]`` of ``x1, y1, x2, y2, conf, cls``.

        Rows arrive sorted by descending confidence, so the first row below the threshold means every
        remaining row is too — hence the early break rather than a full scan of 300 rows per frame.
        """
        raw = outputs[0]
        # The end-to-end head's last dimension is the per-detection field count: 6 for detection,
        # 38 for segmentation. The one-to-many head is (1, nc+4, 8400), where the last dimension is
        # the anchor count — so an upper bound is what actually distinguishes them.
        if raw.ndim != 3 or not 6 <= raw.shape[-1] <= 64:
            raise ValueError(
                f"unexpected detection output shape {raw.shape}; expected (1, N, 6) from a "
                "YOLO26 end-to-end export. A model exported with end2end=False produces "
                "(1, nc+4, 8400) and needs NMS — re-export with the default head."
            )

        results: list[VisionResult] = []
        for row in raw[0]:
            confidence = float(row[4])
            if confidence < self.conf_threshold:
                break
            x1, y1, x2, y2 = letterbox.invert(
                float(row[0]), float(row[1]), float(row[2]), float(row[3])
            )
            if x2 - x1 < 1.0 or y2 - y1 < 1.0:
                continue  # collapsed by clipping: the object is outside the frame
            class_index = int(row[5])
            results.append(
                VisionResult(
                    label=self.names.get(class_index, str(class_index)),
                    confidence=confidence,
                    bbox=BBox(x1=x1, y1=y1, x2=x2, y2=y2),
                    attrs={"class_index": class_index},
                )
            )
            if len(results) >= self.max_detections:
                break
        return results

    def warmup(self) -> None:
        dummy = np.zeros((self.imgsz, self.imgsz, 3), dtype=np.uint8)
        self.detect(dummy)

    def close(self) -> None:
        self.session = None  # type: ignore[assignment]


class OnnxYoloSegDetector(OnnxYoloDetector):
    """YOLO26 instance segmentation.

    Output is ``[1, 300, 38]`` — the same six values plus 32 mask coefficients — and ``[1, 32, 160,
    160]`` mask prototypes. A mask is the coefficient-weighted sum of the prototypes, sigmoid'd,
    cropped to the box. Used instead of SAM 3.1 on CPU (PRD open question Q3).
    """

    def __init__(self, *args: Any, mask_threshold: float = 0.5, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.mask_threshold = mask_threshold

    def _postprocess(self, outputs: list[np.ndarray], letterbox: Letterbox) -> list[VisionResult]:
        raw = outputs[0]
        protos = outputs[1] if len(outputs) > 1 else None
        if raw.ndim != 3 or raw.shape[-1] < 38 or protos is None:
            # Fall back to plain detection rather than failing: a seg model that behaves like a
            # detect model is still useful, and the operator should see boxes rather than nothing.
            log.warning("seg.unexpected_output", shape=raw.shape)
            return super()._postprocess(outputs, letterbox)

        proto = protos[0]  # (32, 160, 160)
        proto_channels, proto_height, proto_width = proto.shape
        flat = proto.reshape(proto_channels, -1)

        results: list[VisionResult] = []
        for row in raw[0]:
            confidence = float(row[4])
            if confidence < self.conf_threshold:
                break
            x1, y1, x2, y2 = letterbox.invert(
                float(row[0]), float(row[1]), float(row[2]), float(row[3])
            )
            if x2 - x1 < 1.0 or y2 - y1 < 1.0:
                continue
            coefficients = row[6:38].astype(np.float32)
            mask = 1.0 / (1.0 + np.exp(-(coefficients @ flat)))
            mask = mask.reshape(proto_height, proto_width)
            mask_rle = self._crop_and_encode(mask, letterbox, (x1, y1, x2, y2))
            class_index = int(row[5])
            results.append(
                VisionResult(
                    label=self.names.get(class_index, str(class_index)),
                    confidence=confidence,
                    bbox=BBox(x1=x1, y1=y1, x2=x2, y2=y2),
                    mask_rle=mask_rle,
                    attrs={"class_index": class_index, "has_mask": mask_rle is not None},
                )
            )
            if len(results) >= self.max_detections:
                break
        return results

    def _crop_and_encode(
        self, mask: np.ndarray, letterbox: Letterbox, box: tuple[float, float, float, float]
    ) -> str | None:
        """Crop the prototype mask to the box and RLE-encode it.

        RLE rather than a PNG: masks are stored per detection at up to 2 fps per camera, and a
        binary mask compresses to a few hundred bytes as runs while a PNG carries a header, a
        palette and a zlib stream for the same information.
        """
        scale = letterbox.size / mask.shape[0]
        # Box back into letterboxed space, then into prototype space.
        lx1 = (box[0] * letterbox.scale + letterbox.pad_x) / scale
        ly1 = (box[1] * letterbox.scale + letterbox.pad_y) / scale
        lx2 = (box[2] * letterbox.scale + letterbox.pad_x) / scale
        ly2 = (box[3] * letterbox.scale + letterbox.pad_y) / scale

        x1, y1 = max(0, int(lx1)), max(0, int(ly1))
        x2, y2 = min(mask.shape[1], int(np.ceil(lx2))), min(mask.shape[0], int(np.ceil(ly2)))
        if x2 - x1 < 1 or y2 - y1 < 1:
            return None
        cropped = mask[y1:y2, x1:x2] > self.mask_threshold
        if not cropped.any():
            return None
        return encode_rle(cropped)


def encode_rle(mask: np.ndarray) -> str:
    """Encode a boolean mask as ``"height,width,run0,run1,..."`` starting from False.

    A compact, dependency-free format that any consumer can decode in a few lines — deliberately
    not pycocotools, which would be a wheel and a build dependency for a hundred lines of maths.
    """
    height, width = mask.shape
    flat = mask.reshape(-1).astype(np.uint8)
    # Run boundaries via diff; prepend a False so a mask starting True yields a leading zero run.
    padded = np.concatenate(([0], flat, [0]))
    changes = np.flatnonzero(padded[1:] != padded[:-1])
    runs = np.diff(np.concatenate(([0], changes)))
    return f"{height},{width}," + ",".join(str(int(run)) for run in runs)


def decode_rle(encoded: str) -> np.ndarray:
    """Inverse of :func:`encode_rle`."""
    parts = encoded.split(",")
    height, width = int(parts[0]), int(parts[1])
    runs = [int(value) for value in parts[2:]]
    flat = np.zeros(height * width, dtype=bool)
    position = 0
    value = False
    for run in runs:
        if value and run:
            flat[position : position + run] = True
        position += run
        value = not value
    return flat.reshape(height, width)


class OnnxReidEmbedder:
    """512-d appearance embeddings from ``yolo26n-reid.onnx``.

    Used for two things that both hinge on "is this the same object": recovering a track through an
    occlusion, and associating one truck seen by two cameras. The input is dynamic-sized, so crops
    are resized to a fixed 128x256 (the usual person-ReID aspect) for batching consistency.
    """

    CROP_WIDTH = 128
    CROP_HEIGHT = 256

    def __init__(
        self,
        model_path: Path | str,
        *,
        threads: int = 2,
        providers: list[str] | None = None,
    ) -> None:
        self.model_path = Path(model_path)
        self.name = self.model_path.name
        self.session = _load_session(self.model_path, threads=threads, providers=providers)
        self.input_name = self.session.get_inputs()[0].name
        output_shape = self.session.get_outputs()[0].shape
        self.dim = int(output_shape[-1]) if isinstance(output_shape[-1], int) else 512
        log.info("reid.loaded", model=self.name, dim=self.dim)

    def embed_crops(self, crops: list[np.ndarray]) -> list[list[float]]:
        """Embed several crops in one inference pass."""
        if not crops:
            return []
        batch = np.stack([self._prepare(crop) for crop in crops])
        vectors = self.session.run(None, {self.input_name: batch})[0]
        return [self._normalise(vector) for vector in vectors]

    def embed_crop(self, crop: np.ndarray) -> list[float]:
        return self.embed_crops([crop])[0]

    def _prepare(self, crop: np.ndarray) -> np.ndarray:
        resized = cv2.resize(
            crop, (self.CROP_WIDTH, self.CROP_HEIGHT), interpolation=cv2.INTER_LINEAR
        )
        tensor = resized[:, :, ::-1].transpose(2, 0, 1).astype(np.float32) / 255.0
        return np.ascontiguousarray(tensor)

    @staticmethod
    def _normalise(vector: np.ndarray) -> list[float]:
        """L2-normalise, so cosine similarity is a dot product and thresholds are comparable."""
        norm = float(np.linalg.norm(vector))
        if norm == 0:
            return [0.0] * len(vector)
        return (vector / norm).astype(float).tolist()

    def close(self) -> None:
        self.session = None  # type: ignore[assignment]


def crop_with_context(image: np.ndarray, bbox: BBox, *, ratio: float = 0.08) -> np.ndarray:
    """Crop a detection with a little surrounding context.

    ReID benefits from a few pixels of background: a box cropped exactly to the object throws away
    the silhouette edge, which is part of what makes two views of the same vehicle match.
    """
    height, width = image.shape[:2]
    expanded = bbox.expand(ratio).clip(width, height)
    y1, y2 = int(expanded.y1), max(int(expanded.y2), int(expanded.y1) + 1)
    x1, x2 = int(expanded.x1), max(int(expanded.x2), int(expanded.x1) + 1)
    return image[y1:y2, x1:x2]
