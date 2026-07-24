"""Tests for the vision engine.

Most of these need no model weights: the decode is tested against hand-built tensors, which is both
faster and *stricter* than running a model — a synthetic tensor lets me assert the exact pixel a box
should land on, where a real inference only lets me assert that the answer looks reasonable.

The tests that do need weights are marked ``models`` and skip when ``.sio/models`` is empty, so the
unit ring still runs on a laptop that has never downloaded 186 MB.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from sio_core.config import Settings
from sio_core.ports import VisionResult
from sio_schemas import BBox

pytest.importorskip("cv2", reason="opencv is a perception dependency")

import cv2
from sio_perception.detectors.fire import (
    FireHeuristicDetector,
    ThermalFireCorroborator,
)
from sio_perception.detectors.onnx_yolo import (
    Letterbox,
    OnnxYoloDetector,
    decode_rle,
    encode_rle,
)
from sio_perception.detectors.synthetic import NullDetector, SyntheticDetector
from sio_perception.factory import build_detector, build_fire_detector
from sio_perception.redact import Redactor

MODEL_DIR = Path(".sio/models")
DETECT_MODEL = MODEL_DIR / "yolo26n.onnx"
SAMPLE = Path(".sio/samples/bus.jpg")

needs_models = pytest.mark.skipif(
    not DETECT_MODEL.exists(), reason="model weights absent; run: just models"
)
needs_sample = pytest.mark.skipif(not SAMPLE.exists(), reason="sample image absent")


# ------------------------------------------------------------------------ letterbox
def test_letterbox_preserves_aspect_and_centres() -> None:
    box = Letterbox((720, 1280), 640)
    assert box.scale == pytest.approx(0.5)
    assert (box.new_width, box.new_height) == (640, 360)
    assert box.pad_x == 0
    assert box.pad_y == pytest.approx(140.0), "the image must be centred, not top-aligned"


def test_letterbox_round_trips_a_box() -> None:
    """The inversion is the part that matters: a wrong one yields plausible boxes that are all off."""
    box = Letterbox((720, 1280), 640)
    # A box at (100, 200)-(300, 400) in source pixels, mapped forward by hand...
    forward = (
        100 * box.scale + box.pad_x,
        200 * box.scale + box.pad_y,
        300 * box.scale + box.pad_x,
        400 * box.scale + box.pad_y,
    )
    # ...and back again.
    x1, y1, x2, y2 = box.invert(*forward)
    assert (x1, y1, x2, y2) == pytest.approx((100, 200, 300, 400), abs=0.01)


def test_letterbox_clips_to_the_source_image() -> None:
    box = Letterbox((720, 1280), 640)
    x1, y1, x2, y2 = box.invert(-50, -50, 5000, 5000)
    assert (x1, y1) == (0.0, 0.0)
    assert (x2, y2) == (1280.0, 720.0)


def test_letterbox_applies_padding_with_the_ultralytics_grey() -> None:
    image = np.full((360, 640, 3), 200, dtype=np.uint8)
    box = Letterbox(image.shape[:2], 640)
    canvas = box.apply(image)
    assert canvas.shape == (640, 640, 3)
    assert canvas[0, 0].tolist() == [114, 114, 114], "pad colour must match the training-time pad"
    assert canvas[320, 320].tolist() == [200, 200, 200]


# --------------------------------------------------------------------- the decode
class FakeSession:
    """Minimal stand-in for an onnxruntime session, so the decode can be tested exactly."""

    def __init__(self, output: np.ndarray, names: dict[int, str] | None = None) -> None:
        self._output = output
        self._names = names or {0: "person", 7: "truck"}

    def get_inputs(self):
        return [type("Input", (), {"name": "images", "shape": [1, 3, 640, 640]})()]

    def get_outputs(self):
        return [type("Output", (), {"name": "output0", "shape": list(self._output.shape)})()]

    def get_modelmeta(self):
        names = str(self._names)
        return type("Meta", (), {"custom_metadata_map": {"names": names}})()

    def get_providers(self):
        return ["CPUExecutionProvider"]

    def run(self, _outputs, _feeds):
        return [self._output]


def detector_with(output: np.ndarray, **kwargs: object) -> OnnxYoloDetector:
    """An OnnxYoloDetector whose session is faked, so no weights are needed."""
    detector = OnnxYoloDetector.__new__(OnnxYoloDetector)
    detector.model_path = Path("fake.onnx")
    detector.name = "fake.onnx"
    detector.conf_threshold = float(kwargs.get("conf_threshold", 0.35))
    detector.imgsz = int(kwargs.get("imgsz", 640))
    detector.max_detections = int(kwargs.get("max_detections", 300))
    detector.session = FakeSession(output)  # type: ignore[assignment]
    detector.input_name = "images"
    detector.names = {0: "person", 7: "truck"}
    return detector


def test_decode_maps_classes_and_inverts_the_letterbox() -> None:
    """The end-to-end head emits x1,y1,x2,y2,conf,cls in letterboxed space."""
    # A 640x640 letterbox of a 640x640 image is the identity, which keeps the arithmetic obvious.
    output = np.zeros((1, 3, 6), dtype=np.float32)
    output[0, 0] = [100, 120, 200, 320, 0.91, 7]  # a truck
    output[0, 1] = [300, 200, 340, 400, 0.77, 0]  # a person
    output[0, 2] = [0, 0, 10, 10, 0.10, 0]  # below threshold

    detector = detector_with(output)
    results = detector.detect(np.zeros((640, 640, 3), dtype=np.uint8))

    assert [r.label for r in results] == ["truck", "person"], (
        "the low-confidence row must be dropped"
    )
    assert results[0].confidence == pytest.approx(0.91)
    assert results[0].bbox == BBox(x1=100, y1=120, x2=200, y2=320)
    assert results[0].attrs["class_index"] == 7


def test_decode_stops_at_the_first_row_below_threshold() -> None:
    """Rows arrive confidence-descending, so scanning past the first miss is wasted work."""
    output = np.zeros((1, 300, 6), dtype=np.float32)
    output[0, 0] = [10, 10, 20, 20, 0.9, 0]
    output[0, 1] = [10, 10, 20, 20, 0.2, 0]
    output[0, 2] = [10, 10, 20, 20, 0.8, 0]  # would qualify, but comes after a miss
    results = detector_with(output).detect(np.zeros((640, 640, 3), dtype=np.uint8))
    assert len(results) == 1


def test_decode_scales_boxes_for_a_non_square_image() -> None:
    output = np.zeros((1, 1, 6), dtype=np.float32)
    # Centre of the letterboxed canvas for a 1280x720 source: scale 0.5, pad_y 140.
    output[0, 0] = [320 - 50, 320 - 50, 320 + 50, 320 + 50, 0.8, 0]
    results = detector_with(output).detect(np.zeros((720, 1280, 3), dtype=np.uint8))
    box = results[0].bbox
    assert box.center == pytest.approx((640.0, 360.0), abs=1.0), "should map to the image centre"
    assert box.width == pytest.approx(200.0, abs=1.0), (
        "100 letterboxed px at scale 0.5 = 200 source px"
    )


def test_decode_drops_boxes_that_clip_away_entirely() -> None:
    output = np.zeros((1, 1, 6), dtype=np.float32)
    output[0, 0] = [-100, -100, -90, -90, 0.9, 0]  # entirely off-frame
    assert detector_with(output).detect(np.zeros((640, 640, 3), dtype=np.uint8)) == []


def test_decode_rejects_a_one_to_many_export() -> None:
    """A model exported with end2end=False emits (1, nc+4, 8400) and needs NMS; say so clearly."""
    output = np.zeros((1, 84, 8400), dtype=np.float32)
    with pytest.raises(ValueError, match="end2end"):
        detector_with(output).detect(np.zeros((640, 640, 3), dtype=np.uint8))


def test_decode_honours_max_detections() -> None:
    output = np.full((1, 300, 6), 0.0, dtype=np.float32)
    for index in range(300):
        output[0, index] = [10, 10, 40, 40, 0.9, 0]
    results = detector_with(output, max_detections=17).detect(
        np.zeros((640, 640, 3), dtype=np.uint8)
    )
    assert len(results) == 17


def test_empty_image_yields_nothing() -> None:
    detector = detector_with(np.zeros((1, 1, 6), dtype=np.float32))
    assert detector.detect(np.zeros((0, 0, 3), dtype=np.uint8)) == []


# ------------------------------------------------------------------------- masks
def test_rle_round_trips() -> None:
    mask = np.zeros((10, 12), dtype=bool)
    mask[2:6, 3:9] = True
    mask[8, 0] = True
    encoded = encode_rle(mask)
    assert np.array_equal(decode_rle(encoded), mask)


def test_rle_handles_a_mask_that_starts_true() -> None:
    mask = np.ones((4, 4), dtype=bool)
    assert np.array_equal(decode_rle(encode_rle(mask)), mask)


def test_rle_is_compact() -> None:
    """A binary mask is runs, not an image — that is why this is not a PNG."""
    mask = np.zeros((160, 160), dtype=bool)
    mask[40:120, 40:120] = True
    encoded = encode_rle(mask)
    assert len(encoded) < 1200, f"expected a few hundred bytes, got {len(encoded)}"


# -------------------------------------------------------------------------- fire
def flame_frame(seed: int, size: tuple[int, int] = (360, 640)) -> np.ndarray:
    """A ragged, flame-like bright region."""
    image = np.full((*size, 3), 60, np.uint8)
    image[:, :, :] = (60, 62, 65)
    rng = np.random.default_rng(seed)
    for _ in range(70):
        x = int(rng.integers(250, 390))
        y = int(rng.integers(130, 260))
        radius = int(rng.integers(5, 24))
        cv2.circle(image, (x, y), radius, (30, 120, 245), -1)
    return image


def red_rect_frame(offset: int = 0, size: tuple[int, int] = (360, 640)) -> np.ndarray:
    """A rigid red rectangle — a truck, a container, a hi-vis panel."""
    image = np.full((*size, 3), 60, np.uint8)
    image[:, :, :] = (60, 62, 65)
    cv2.rectangle(image, (250 + offset, 140), (390 + offset, 260), (40, 40, 200), -1)
    return image


def count_fire(detector: FireHeuristicDetector, frames: list[np.ndarray], source: str) -> int:
    return sum(
        1
        for frame in frames
        for result in detector.detect(frame, source_id=source)
        if result.label == "fire"
    )


def test_a_stationary_red_object_is_not_fire() -> None:
    """Colour alone must never raise a fire alarm. The first version of this detector did."""
    detector = FireHeuristicDetector()
    assert count_fire(detector, [red_rect_frame() for _ in range(10)], "static") == 0
    assert detector._suppressed > 0, "suppression should be recorded for diagnosis"


def test_a_moving_red_object_is_not_fire() -> None:
    """The subtler case: a translating rectangle produces a big frame difference.

    Frame-difference flicker cannot tell a driving red truck from a flame, which is exactly why the
    detector also requires shape irregularity — that measure is translation-invariant.
    """
    detector = FireHeuristicDetector()
    frames = [red_rect_frame(offset=index * 9) for index in range(10)]
    assert count_fire(detector, frames, "moving") == 0


def test_a_flickering_flame_is_fire() -> None:
    detector = FireHeuristicDetector()
    frames = [flame_frame(index) for index in range(10)]
    assert count_fire(detector, frames, "flame") >= 5


def test_fire_detections_carry_their_reasoning() -> None:
    """An operator has to be able to see *why* a heuristic fired."""
    detector = FireHeuristicDetector()
    found: VisionResult | None = None
    for index in range(10):
        for result in detector.detect(flame_frame(index), source_id="flame"):
            if result.label == "fire":
                found = result
    assert found is not None
    assert found.attrs["heuristic"] is True
    for key in ("colour_score", "flicker_score", "irregularity_score", "growth_score", "method"):
        assert key in found.attrs, f"missing {key} from the evidence"
    assert "stand-in" in found.attrs["note"], "the detection must not pass as a trained model"


def test_flicker_history_is_per_camera() -> None:
    """One shared history would compare Gate A's last frame with Dock 3's current one."""
    detector = FireHeuristicDetector()
    for index in range(6):
        detector.detect(flame_frame(index), source_id="cam-a")
    # A first frame on a second camera has no history, so it cannot show flicker yet.
    results = detector.detect(flame_frame(99), source_id="cam-b")
    assert [r for r in results if r.label == "fire"] == []


def test_thermal_corroboration_is_bounded() -> None:
    """Corroboration should turn a maybe into a probably, not manufacture certainty."""
    corroborator = ThermalFireCorroborator(baseline_c=22.0, alarm_delta_c=15.0)
    assert corroborator.corroboration(21.0)[0] == 1.0
    assert corroborator.corroboration(None)[0] == 1.0
    boosted, evidence = corroborator.corroboration(90.0)
    assert 1.0 < boosted <= 1.6
    assert evidence["thermal"] == "elevated"


# --------------------------------------------------------------------- synthetic
def test_synthetic_detector_reads_ground_truth() -> None:
    detector = SyntheticDetector(drop_rate=0.0, jitter_px=0.0, seed=1)
    payload = {
        "width": 1280,
        "height": 720,
        "visible": [
            {
                "agent_id": "truck-1",
                "class": "truck",
                "bbox": [100, 200, 300, 400],
                "distance_m": 12,
            },
            {
                "agent_id": "worker-1",
                "class": "person",
                "bbox": [500, 300, 540, 420],
                "distance_m": 30,
            },
        ],
    }
    results = detector.detect_from_payload(payload)
    assert [r.label for r in results] == ["truck", "person"]
    assert all(r.attrs["synthetic"] is True for r in results)
    assert results[0].confidence > results[1].confidence, "confidence should fall with distance"


def test_synthetic_detector_is_imperfect_on_purpose() -> None:
    """Perfect boxes would make the tracker's job trivial and hide association bugs."""
    detector = SyntheticDetector(drop_rate=0.5, jitter_px=4.0, seed=7)
    payload = {
        "width": 1280,
        "height": 720,
        "visible": [
            {
                "agent_id": f"a{i}",
                "class": "person",
                "bbox": [10 * i, 100, 10 * i + 40, 300],
                "distance_m": 20,
            }
            for i in range(40)
        ],
    }
    results = detector.detect_from_payload(payload)
    assert 5 < len(results) < 40, "some detections should be dropped, not all or none"
    boxes = {(round(r.bbox.x1, 2), round(r.bbox.y1, 2)) for r in results}
    assert len(boxes) == len(results), "jitter should make boxes distinct"


def test_synthetic_detector_emits_the_injected_fire() -> None:
    detector = SyntheticDetector(drop_rate=0.0)
    results = detector.detect_from_payload(
        {"width": 1280, "height": 720, "visible": [], "fire": True}
    )
    assert [r.label for r in results] == ["fire"]


def test_synthetic_detector_refuses_to_work_from_pixels() -> None:
    """It reads ground truth; pretending to infer from an image would be a lie."""
    with pytest.raises(NotImplementedError, match="ground truth"):
        SyntheticDetector().detect(np.zeros((10, 10, 3), dtype=np.uint8))


def test_null_detector_detects_nothing() -> None:
    assert NullDetector().detect(np.zeros((64, 64, 3), dtype=np.uint8)) == []


# ----------------------------------------------------------------------- factory
def test_auto_falls_back_to_synthetic_without_weights() -> None:
    settings = Settings(_env_file=None, detector="auto", model_dir=Path("/nonexistent"))  # type: ignore[call-arg]
    assert build_detector(settings).name == "synthetic"


def test_explicit_kinds_are_honoured() -> None:
    assert build_detector(Settings(_env_file=None, detector="synthetic")).name == "synthetic"  # type: ignore[call-arg]
    assert build_detector(Settings(_env_file=None, detector="null")).name == "null"  # type: ignore[call-arg]


def test_deepstream_points_at_the_real_gpu_path() -> None:
    """A stub that silently finds nothing is worse than one that refuses to start."""
    from sio_core import ConfigError

    with pytest.raises(ConfigError, match="CUDAExecutionProvider"):
        build_detector(Settings(_env_file=None, detector="deepstream"))  # type: ignore[call-arg]


def test_unknown_detector_is_a_config_error() -> None:
    from sio_core import ConfigError

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    object.__setattr__(settings, "detector", "magic-eye")
    with pytest.raises(ConfigError, match="magic-eye"):
        build_detector(settings)


def test_fire_detector_is_always_built() -> None:
    """Fire is not a COCO class, so it cannot come from the main forward pass."""
    assert build_fire_detector(Settings(_env_file=None)) is not None  # type: ignore[call-arg]


# --------------------------------------------------------------------- redaction
def test_redaction_blurs_faces_and_plates() -> None:
    settings = Settings(_env_file=None, blur_faces=True, blur_plates=True)  # type: ignore[call-arg]
    redactor = Redactor(settings)
    # A sharp, high-contrast pattern, so blurring is measurable rather than assumed.
    image = np.zeros((400, 400, 3), dtype=np.uint8)
    image[::2] = 255
    results = [
        VisionResult(label="person", confidence=0.9, bbox=BBox(x1=50, y1=50, x2=150, y2=350)),
        VisionResult(label="truck", confidence=0.9, bbox=BBox(x1=200, y1=100, x2=380, y2=300)),
    ]
    output, applied = redactor.apply(image, results)
    assert applied == 2

    def variance(img: np.ndarray, box: BBox) -> float:
        patch = img[int(box.y1) : int(box.y2), int(box.x1) : int(box.x2)]
        return float(patch.var())

    face = redactor._face_region(results[0].bbox)
    assert variance(output, face) < variance(image, face) * 0.5, "the face region must lose detail"
    untouched = BBox(x1=0, y1=0, x2=40, y2=40)
    assert variance(output, untouched) == pytest.approx(variance(image, untouched)), (
        "regions outside a detection must be untouched"
    )


def test_redaction_is_a_no_op_when_disabled() -> None:
    settings = Settings(_env_file=None, blur_faces=False, blur_plates=False)  # type: ignore[call-arg]
    redactor = Redactor(settings)
    image = np.zeros((100, 100, 3), dtype=np.uint8)
    output, applied = redactor.apply(
        image, [VisionResult(label="person", confidence=0.9, bbox=BBox(x1=1, y1=1, x2=50, y2=90))]
    )
    assert applied == 0
    assert output is image, "the no-op path must not copy every frame"


def test_face_region_covers_the_top_of_a_person_box() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    region = Redactor(settings)._face_region(BBox(x1=100, y1=200, x2=200, y2=600))
    assert region.y1 == 200
    assert region.y2 < 400, "the face is in the upper part of the body, not the whole box"
    assert region.x1 > 100 and region.x2 < 200, "horizontally inset"


# -------------------------------------------------------- real weights (optional)
@needs_models
@needs_sample
def test_real_model_finds_the_expected_objects() -> None:
    """The canonical Ultralytics test image: one bus and several people.

    This is the test that would catch a genuinely wrong decode — a transposed output, a bad class
    map, an inverted letterbox — in a way a synthetic tensor cannot.
    """
    detector = OnnxYoloDetector(DETECT_MODEL, conf_threshold=0.4, threads=2)
    image = cv2.imread(str(SAMPLE))
    height, width = image.shape[:2]
    results = detector.detect(image)

    labels = [result.label for result in results]
    assert "bus" in labels, f"expected a bus in bus.jpg, got {labels}"
    assert labels.count("person") >= 3, f"expected several people, got {labels}"

    for result in results:
        box = result.bbox
        assert 0 <= box.x1 < box.x2 <= width
        assert 0 <= box.y1 < box.y2 <= height
        assert box.area > 100, "a detection covering a handful of pixels is a decode error"

    bus = next(result for result in results if result.label == "bus")
    assert bus.bbox.area > (width * height) * 0.3, "the bus dominates this frame"
    assert bus.confidence > 0.8


@needs_models
def test_class_names_come_from_the_model_file() -> None:
    """A separate labels file would drift from the weights and mislabel everything."""
    detector = OnnxYoloDetector(DETECT_MODEL, threads=1)
    assert len(detector.names) == 80, "COCO has 80 classes"
    assert detector.names[0] == "person"
    assert detector.names[7] == "truck"


# ------------------------------------------------------------------- staleness
def test_stale_frames_are_skipped() -> None:
    """A restart replays the stream; inferring on an hour-old frame helps nobody.

    Without this guard the live picture sits minutes behind while the service grinds through a
    backlog it can never catch up with — observed for real: 23,986 replayed frame observations after
    a phase's worth of runs.
    """
    from sio_perception.service import PerceptionService

    service = PerceptionService.__new__(PerceptionService)
    service.settings = Settings(_env_file=None, perception_max_age_s=60.0)  # type: ignore[call-arg]
    service._stale_skipped = 0
    service._warned_stale = False
    service.log = type("L", (), {"warning": lambda *a, **k: None})()  # type: ignore[assignment]

    assert service._is_stale(5.0) is False
    assert service._is_stale(59.9) is False
    assert service._is_stale(120.0) is True
    assert service._is_stale(3600.0) is True
    assert service._stale_skipped == 2


def test_frame_rate_cap_is_per_camera() -> None:
    """A global cap lets a busy camera starve a quiet one — and the quiet one watches the gate
    nobody uses, which is exactly where an intrusion happens."""
    from sio_perception.service import PerceptionService

    service = PerceptionService.__new__(PerceptionService)
    service.settings = Settings(_env_file=None, perception_fps=2.0)  # type: ignore[call-arg]
    service._last_inference_at = {}

    assert service._due("cam-a") is True
    assert service._due("cam-a") is False, "same camera, too soon"
    assert service._due("cam-b") is True, "a different camera must not be blocked"
