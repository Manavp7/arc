"""CLIP embeddings on ONNX Runtime: images and text in one shared vector space.

This is what makes "show me the red truck at the gate" a query rather than a feature request. Both
towers of CLIP project into the same 512-d space, so a text query and a stored frame are directly
comparable by cosine similarity.

Lives in ``sio_core`` rather than in a service because **two** services need it and they must agree:
``perception`` embeds frames as they arrive, ``worldmodel`` embeds the query when someone searches. If
they used different models the vectors would be incomparable and search would return confident
nonsense.

No transformers, no torch: the ONNX exports plus the ``tokenizers`` package. 512 dimensions matches
the YOLO26 ReID head, so one ``vector(512)`` column serves frame search and re-identification.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

from ..errors import ModelUnavailable
from ..telemetry import get_logger

log = get_logger("sio.vision.clip")

# CLIP ViT-B/32 preprocessing, from the model's preprocessor_config.json. These constants are not
# arbitrary: the model was trained on images normalised exactly this way, and getting them wrong
# produces embeddings that are stable, plausible and wrong.
CLIP_SIZE = 224
CLIP_MEAN = (0.48145466, 0.4578275, 0.40821073)
CLIP_STD = (0.26862954, 0.26130258, 0.27577711)
CLIP_CONTEXT_LENGTH = 77


class OnnxClipEmbedder:
    """Image and text embeddings from the CLIP ONNX exports."""

    name = "clip-vit-base-patch32"
    dim = 512

    def __init__(
        self,
        vision_model: Path | str,
        text_model: Path | str,
        tokenizer_path: Path | str,
        *,
        threads: int = 2,
        providers: list[str] | None = None,
    ) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - declared in the embeddings extra
            raise ModelUnavailable(
                "onnxruntime is not installed; install the embeddings extra or run: just setup"
            ) from exc
        try:
            from tokenizers import Tokenizer
        except ImportError as exc:  # pragma: no cover
            raise ModelUnavailable("the tokenizers package is not installed") from exc

        for path in (vision_model, text_model, tokenizer_path):
            if not Path(path).exists():
                raise ModelUnavailable(f"CLIP asset not found: {path}\nrun: just models")

        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        chosen = providers or ["CPUExecutionProvider"]
        available = set(ort.get_available_providers())
        usable = [name for name in chosen if name in available] or ["CPUExecutionProvider"]

        self.vision = ort.InferenceSession(str(vision_model), options, providers=usable)
        self.text = ort.InferenceSession(str(text_model), options, providers=usable)
        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self._vision_input = self.vision.get_inputs()[0].name
        self._text_inputs = [inp.name for inp in self.text.get_inputs()]
        # Read the real output width rather than trusting the constant: a different CLIP variant
        # would silently produce vectors of another size and corrupt the pgvector column.
        output_shape = self.vision.get_outputs()[0].shape
        if isinstance(output_shape[-1], int):
            self.dim = int(output_shape[-1])
        log.info(
            "clip.loaded",
            dim=self.dim,
            text_inputs=self._text_inputs,
            providers=self.vision.get_providers(),
        )

    # ------------------------------------------------------------------- images
    def embed_image(self, image: Any) -> list[float]:
        """Embed a BGR image array (OpenCV convention)."""
        return self.embed_images([image])[0]

    def embed_images(self, images: list[Any]) -> list[list[float]]:
        import numpy as np

        if not images:
            return []
        batch = np.stack([self._preprocess_image(image) for image in images])
        vectors = self.vision.run(None, {self._vision_input: batch})[0]
        return [_l2_normalise(vector) for vector in vectors]

    def _preprocess_image(self, image: Any) -> Any:
        import cv2
        import numpy as np

        # Centre-crop to square first, then resize: a plain resize squashes a 16:9 frame and CLIP
        # was never trained on squashed images.
        height, width = image.shape[:2]
        side = min(height, width)
        top = (height - side) // 2
        left = (width - side) // 2
        square = image[top : top + side, left : left + side]

        resized = cv2.resize(square, (CLIP_SIZE, CLIP_SIZE), interpolation=cv2.INTER_CUBIC)
        rgb = resized[:, :, ::-1].astype(np.float32) / 255.0
        normalised = (rgb - np.array(CLIP_MEAN, dtype=np.float32)) / np.array(
            CLIP_STD, dtype=np.float32
        )
        return np.ascontiguousarray(normalised.transpose(2, 0, 1))

    # --------------------------------------------------------------------- text
    def embed_text(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        import numpy as np

        if not texts:
            return []
        input_ids = np.zeros((len(texts), CLIP_CONTEXT_LENGTH), dtype=np.int64)
        attention = np.zeros((len(texts), CLIP_CONTEXT_LENGTH), dtype=np.int64)
        for row, text in enumerate(texts):
            encoded = self.tokenizer.encode(text)
            ids = encoded.ids[:CLIP_CONTEXT_LENGTH]
            input_ids[row, : len(ids)] = ids
            attention[row, : len(ids)] = 1

        feeds: dict[str, Any] = {}
        for name in self._text_inputs:
            if "attention" in name:
                feeds[name] = attention
            else:
                feeds[name] = input_ids
        vectors = self.text.run(None, feeds)[0]
        return [_l2_normalise(vector) for vector in vectors]

    def close(self) -> None:
        # The sessions are typed Any (onnxruntime is an untyped import), so no ignore is needed —
        # and mypy flags a redundant one.
        self.vision = None
        self.text = None


class HashEmbedder:
    """Deterministic pseudo-embeddings from a hash.

    For tests and for running without model weights. It is **not** semantic — two images of the same
    truck get unrelated vectors — so it is named for what it is, and anything that depends on meaning
    (semantic search) will visibly fail rather than quietly return rubbish, which is the point.
    """

    name = "hash"
    dim = 512

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def embed_image(self, image: Any) -> list[float]:
        try:
            payload = bytes(image.tobytes()[:4096])
        except AttributeError:
            payload = str(image).encode()[:4096]
        return self._vector(payload)

    def embed_text(self, text: str) -> list[float]:
        # Token-based rather than whole-string, so at least *identical* queries match and queries
        # sharing words are somewhat closer than unrelated ones.
        tokens = sorted(set(text.lower().split()))
        payload = " ".join(tokens).encode()
        return self._vector(payload)

    def _vector(self, payload: bytes) -> list[float]:
        values: list[float] = []
        counter = 0
        while len(values) < self.dim:
            digest = hashlib.sha256(payload + counter.to_bytes(4, "big")).digest()
            values.extend((byte - 127.5) / 127.5 for byte in digest)
            counter += 1
        vector = values[: self.dim]
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector

    def close(self) -> None:
        return None


def _l2_normalise(vector: Any) -> list[float]:
    """Unit-normalise, so cosine similarity is a dot product and thresholds are comparable."""
    import numpy as np

    array = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(array))
    if norm == 0:
        return [0.0] * array.size
    return (array / norm).astype(float).tolist()
