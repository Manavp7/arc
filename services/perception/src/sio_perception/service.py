"""Perception service: frames in, detections out (PRD M3)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

import numpy as np
from fastapi import FastAPI

from sio_core import MessageContext, SioService, describe_error, get_blob
from sio_core.ports import VisionResult
from sio_schemas import BusMessage, Detection, Observation, Topic, utc_now

from .detectors.fire import FireHeuristicDetector
from .detectors.onnx_yolo import crop_with_context
from .detectors.synthetic import SyntheticDetector
from .factory import build_detector, build_fire_detector, build_reid
from .redact import Redactor


class PerceptionService(SioService):
    """Consumes ``raw.frames``, runs the vision stack, publishes ``detections``.

    Three design points that matter more than the model choice:

    **Inference runs in a worker thread.** A 25-50 ms forward pass on the event loop would stall bus
    consumption for that whole time, and at 2 fps across eight cameras that is a third of the loop
    spent blocked. ``asyncio.to_thread`` keeps consumption responsive.

    **Frames are sampled, not all processed.** ``SIO_PERCEPTION_FPS`` caps per-camera throughput and
    lag is checked against it; a laptop cannot infer every frame from eight cameras and pretending
    otherwise just grows the backlog until Redis trims it and data is silently lost.

    **Redaction happens before storage.** Faces and plates are blurred on the way in (PRD §14), not
    on the way out, so an un-redacted frame never exists in the object store.
    """

    name = "perception"
    subscribes = (Topic.RAW_FRAMES,)
    tick_interval_s = 30.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.blob = get_blob(self.settings)
        self.detector = build_detector(self.settings)
        self.fire_detector: FireHeuristicDetector | None = build_fire_detector(self.settings)
        self.reid = build_reid(self.settings)
        self.redactor = Redactor(self.settings)
        self._frames_seen = 0
        self._frames_inferred = 0
        self._detections = 0
        self._last_inference_at: dict[str, float] = {}
        self._inference_ms: list[float] = []
        self._decode_failures = 0
        self._stale_skipped = 0
        self._warned_stale = False

    # ----------------------------------------------------------------- lifecycle
    async def setup(self) -> None:
        await asyncio.to_thread(self.detector.warmup)
        self.log.info(
            "perception.ready",
            detector=self.detector.name,
            reid=self.reid.name if self.reid else None,
            fps_cap=self.settings.perception_fps,
            segmentation=self.settings.enable_segmentation,
            blur_faces=self.settings.blur_faces,
            blur_plates=self.settings.blur_plates,
        )

    async def teardown(self) -> None:
        for component in (self.detector, self.reid, self.fire_detector):
            if component is not None:
                with contextlib.suppress(Exception):
                    component.close()

    async def health_checks(self) -> dict[str, str]:
        checks = {"blob": "ok" if await self.blob.ping() else "unreachable"}
        checks["detector"] = f"ok ({self.detector.name})"
        return checks

    async def health_info(self) -> dict[str, str]:
        mean = (
            f"{sum(self._inference_ms) / len(self._inference_ms):.0f} ms"
            if self._inference_ms
            else "n/a"
        )
        return {
            "frames_seen": str(self._frames_seen),
            "frames_inferred": str(self._frames_inferred),
            "detections": str(self._detections),
            "mean_inference": mean,
            "decode_failures": str(self._decode_failures),
            "stale_skipped": str(self._stale_skipped),
        }

    # ------------------------------------------------------------------ handling
    async def on_message(self, message: BusMessage, ctx: MessageContext) -> None:
        if message.kind != "Observation":
            return
        observation = message.decode(Observation)
        self._frames_seen += 1

        if self._is_stale(ctx.age_s):
            return
        if not self._due(observation.source_id):
            return

        results = await self._infer(observation)
        if not results:
            return

        for result in results:
            detection = Detection(
                observation_id=observation.id,
                tenant_id=observation.tenant_id,
                trace_id=observation.trace_id,
                **{"class": result.label},
                bbox=result.bbox,
                mask_ref=result.mask_rle,
                confidence=result.confidence,
                ts=observation.ts,
                source_id=observation.source_id,
                model_name=self.detector.name
                if not result.attrs.get("heuristic")
                else "fire-heuristic",
                attrs=self._detection_attrs(result),
            )
            await ctx.publish(Topic.DETECTIONS, detection)
            self._detections += 1

    def _is_stale(self, age_s: float) -> bool:
        """Skip frames older than the staleness limit.

        A restart replays the stream from the start of the consumer group, and on a busy site that is
        thousands of frames. Inferring on them is not just wasted work: it puts the live picture
        minutes behind while the service processes a past it can never catch up with. The
        observations remain in the timeline; only inference is skipped, and the count is reported so
        the skipping is visible rather than mysterious.
        """
        if age_s <= self.settings.perception_max_age_s:
            return False
        self._stale_skipped += 1
        if not self._warned_stale:
            self._warned_stale = True
            self.log.warning(
                "perception.skipping_stale_frames",
                age_s=round(age_s, 1),
                limit_s=self.settings.perception_max_age_s,
                note="replayed backlog; live frames are unaffected",
            )
        return True

    def _due(self, source_id: str) -> bool:
        """Per-camera frame-rate cap.

        Per *camera*, not global: a global cap would let a busy camera starve a quiet one, and the
        quiet one is often the one watching the gate nobody uses — exactly where an intrusion happens.
        """
        interval = 1.0 / max(0.1, self.settings.perception_fps)
        now = time.monotonic()
        if now - self._last_inference_at.get(source_id, 0.0) < interval:
            return False
        self._last_inference_at[source_id] = now
        return True

    async def _infer(self, observation: Observation) -> list[VisionResult]:
        """Run the vision stack for one frame observation."""
        # The synthetic detector reads the simulator's ground truth from the payload and needs no
        # pixels — which is what lets the whole pipeline run in CI with no weights and no media.
        if isinstance(self.detector, SyntheticDetector):
            results = self.detector.detect_from_payload(observation.payload)
            self._frames_inferred += 1
            return results

        image = await self._load_frame(observation)
        if image is None:
            return []

        started = time.perf_counter()
        results = await asyncio.to_thread(self.detector.detect, image)

        if self.fire_detector is not None:
            fire = await asyncio.to_thread(
                self.fire_detector.detect, image, source_id=observation.source_id
            )
            results = [*results, *fire]

        if self.reid is not None and results:
            results = await asyncio.to_thread(self._attach_embeddings, image, results)

        elapsed = (time.perf_counter() - started) * 1000
        self._inference_ms = [*self._inference_ms[-99:], elapsed]
        self.metrics.inference_seconds.labels(service=self.name, model=self.detector.name).observe(
            elapsed / 1000
        )
        self._frames_inferred += 1

        # Redact and store, so the frame an operator can retrieve is the redacted one.
        await self._store_redacted(observation, image, results)
        return results

    @staticmethod
    def _detection_attrs(result: VisionResult) -> dict[str, Any]:
        """Attributes for the published detection, **including the ReID vector itself**.

        The vector has to travel on the wire: tracking needs it in the same message, and a reference
        it would have to fetch per detection would add a round trip to every association. Rounded to
        four decimals — well inside the noise of an int8-quantised encoder — which roughly halves the
        JSON to ~3.5 kB per detection, about 28 kB/s at eight detections a second. If that ever
        matters the honest next step is int8 plus base64, not dropping the data.

        This existed as a bug first: perception computed the embeddings, recorded only their
        *dimension* in ``attrs``, and discarded the values — so tracking's appearance matching had
        nothing to match on and silently never fired.
        """
        attrs = dict(result.attrs)
        if result.embedding is not None:
            attrs["embedding"] = [round(float(value), 4) for value in result.embedding]
        return attrs

    def _attach_embeddings(
        self, image: np.ndarray, results: list[VisionResult]
    ) -> list[VisionResult]:
        """Add ReID vectors to detections that a tracker could re-identify.

        Only for classes where appearance matching is meaningful. Embedding a `fire` region would
        produce a vector nobody can use, and the crops are the expensive part.
        """
        assert self.reid is not None
        indices = [
            index
            for index, result in enumerate(results)
            if result.label in ("person", "truck", "car", "bus", "forklift", "vehicle")
        ]
        if not indices:
            return results
        crops = [crop_with_context(image, results[index].bbox) for index in indices]
        vectors = self.reid.embed_crops(crops)
        updated = list(results)
        for index, vector in zip(indices, vectors, strict=True):
            original = updated[index]
            updated[index] = VisionResult(
                label=original.label,
                confidence=original.confidence,
                bbox=original.bbox,
                mask_rle=original.mask_rle,
                embedding=tuple(vector),
                attrs={**original.attrs, "reid_dim": len(vector), "reid_model": self.reid.name},
            )
        return updated

    async def _load_frame(self, observation: Observation) -> np.ndarray | None:
        """Fetch and decode the frame this observation refers to.

        A missing frame is a warning, not an error: the object store may be catching up, and dropping
        one frame at 2 fps costs nothing. A frame that is *present but undecodable* is counted
        separately, because that means something upstream is writing rubbish.
        """
        if not observation.raw_ref:
            return None
        try:
            data = await self.blob.get(observation.raw_ref)
        except Exception as exc:
            self.log.debug("frame.missing", key=observation.raw_ref, error=describe_error(exc))
            return None

        import cv2

        buffer = np.frombuffer(data, dtype=np.uint8)
        image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
        if image is None:
            self._decode_failures += 1
            self.log.warning("frame.undecodable", key=observation.raw_ref, bytes=len(data))
            return None
        return image

    async def _store_redacted(
        self, observation: Observation, image: np.ndarray, results: list[VisionResult]
    ) -> None:
        """Blur faces and plates, then store the frame and index it.

        Order matters: redact, then store. Storing first and redacting later means an un-redacted
        frame exists in the object store, and "we deleted it afterwards" is not a privacy posture.
        """
        if not observation.raw_ref:
            return
        redacted, applied = await asyncio.to_thread(self.redactor.apply, image, results)
        if applied:
            import cv2

            success, encoded = cv2.imencode(".jpg", redacted, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            if success:
                await self.blob.put(
                    observation.raw_ref,
                    encoded.tobytes(),
                    content_type="image/jpeg",
                    metadata={"redacted": "true", "regions": str(applied)},
                )

    # ------------------------------------------------------------------ reporting
    async def tick(self) -> None:
        mean = sum(self._inference_ms) / len(self._inference_ms) if self._inference_ms else 0.0
        self.log.info(
            "perception.stats",
            detector=self.detector.name,
            frames_seen=self._frames_seen,
            frames_inferred=self._frames_inferred,
            detections=self._detections,
            stale_skipped=self._stale_skipped,
            mean_inference_ms=round(mean, 1),
            suppressed_fire=self.fire_detector._suppressed if self.fire_detector else 0,
        )

    def routes(self, app: FastAPI) -> None:
        @app.get("/detector", tags=["perception"])
        async def detector_info() -> dict[str, Any]:
            """What is actually running, so a surprising detection can be traced to a model."""
            return {
                "detector": self.detector.name,
                "synthetic": isinstance(self.detector, SyntheticDetector),
                "reid": self.reid.name if self.reid else None,
                "fire_heuristic": self.fire_detector is not None,
                "fps_cap": self.settings.perception_fps,
                "conf_threshold": self.settings.det_conf,
                "providers": getattr(self.detector, "session", None)
                and self.detector.session.get_providers(),  # type: ignore[attr-defined]
                "frames_seen": self._frames_seen,
                "frames_inferred": self._frames_inferred,
                "detections": self._detections,
                "mean_inference_ms": round(sum(self._inference_ms) / len(self._inference_ms), 1)
                if self._inference_ms
                else None,
                "redaction": {
                    "faces": self.settings.blur_faces,
                    "plates": self.settings.blur_plates,
                    "face_recognition_enabled": self.settings.enable_face_recognition,
                },
            }

        @app.post("/detect/sample", tags=["perception"])
        async def detect_sample(key: str) -> dict[str, Any]:
            """Run the detector over one stored frame. Used by the demo and for debugging."""
            observation = Observation(
                source_id="manual", modality="image", ts=utc_now(), raw_ref=key
            )
            results = await self._infer(observation)
            return {
                "key": key,
                "detector": self.detector.name,
                "detections": [
                    {
                        "class": result.label,
                        "confidence": result.confidence,
                        "bbox": result.bbox.to_wire(),
                        "attrs": result.attrs,
                    }
                    for result in results
                ],
            }
