"""Local, bounded video processing. Only pixelated derivatives leave private storage."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from .review_models import (
    MOTION_NAME,
    MOTION_WARNING,
    AnalysisRequest,
    load_pinned_detector,
    pin_profile,
    validate_artifact,
)

MAX_DURATION = 180
SAMPLE_FPS = 2
MAX_FRAMES = MAX_DURATION * SAMPLE_FPS
PLAYBACK_FPS = 15


@lru_cache(maxsize=1)
def media_available() -> bool:
    for executable in ("ffmpeg", "ffprobe"):
        path = shutil.which(executable)
        if path is None:
            return False
        try:
            subprocess.run([path, "-version"], capture_output=True, timeout=5, check=True)
        except (OSError, subprocess.SubprocessError):
            return False
    return True


def probe(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        timeout=15,
        check=True,
    )
    data = json.loads(result.stdout)
    if "mp4" not in data.get("format", {}).get("format_name", ""):
        raise ValueError("The file must contain an MP4 video")
    streams = [stream for stream in data.get("streams", []) if stream.get("codec_type") == "video"]
    if len(streams) != 1:
        raise ValueError("Exactly one video stream is required")
    stream = streams[0]
    duration = float(stream.get("duration") or data["format"].get("duration") or 0)
    width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
    rate = str(stream.get("avg_frame_rate", "0/1")).split("/")
    fps = float(rate[0]) / max(1, float(rate[1])) if len(rate) == 2 else float(rate[0])
    if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION:
        raise ValueError("Video duration must be between 0 and 180 seconds")
    if not 16 <= width <= 1920 or not 16 <= height <= 1080 or not 0 < fps <= 60:
        raise ValueError("Video must be at most 1920x1080 and 60 fps")
    return {"duration_s": round(duration, 3), "width": width, "height": height, "fps": fps}


@lru_cache(maxsize=128)
def playback_frame_rate(path: str, modified_ns: int, size: int) -> float | None:
    """Cache actual derivative metadata by file version; source fps is not playback fps."""
    try:
        return float(probe(Path(path))["fps"])
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return None


def prepare_derivatives(directory: Path, info: dict[str, Any]) -> None:
    width = min(640, info["width"])
    height = max(16, round(width * info["height"] / info["width"] / 2) * 2)
    width = max(16, width // 2 * 2)
    # Fixed 16x16 privacy grid, no audio/metadata/subtitles; the original is not a media endpoint.
    filters = (
        f"scale=16:16:flags=area,scale={width}:{height}:flags=neighbor,setsar=1,fps={PLAYBACK_FPS}"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-protocol_whitelist",
            "file,pipe",
            "-noautorotate",
            "-i",
            str(directory / "original.mp4"),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-t",
            str(MAX_DURATION),
            "-vf",
            filters,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(directory / "playback.mp4"),
        ],
        capture_output=True,
        timeout=120,
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(directory / "playback.mp4"),
            "-frames:v",
            "1",
            "-update",
            "1",
            str(directory / "poster.jpg"),
        ],
        capture_output=True,
        timeout=15,
        check=True,
    )


class RegionTracker:
    """Short-lived geometric region association; IDs never establish personal identity."""

    def __init__(self):
        self.previous: dict[str, dict[str, Any]] = {}
        self.next_id = 1

    def update(self, objects, at):
        available = {key: value for key, value in self.previous.items() if at - value["at"] <= 1.1}
        for obj in objects:
            box = obj["bbox"]
            centre = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
            candidates = []
            for tid, old in available.items():
                if old["class_name"] != obj["class_name"]:
                    continue
                other = old["centre"]
                distance = math.dist(centre, other)
                if distance < 0.22:
                    candidates.append((distance, tid))
            tid = min(candidates)[1] if candidates else f"region-{self.next_id}"
            if not candidates:
                self.next_id += 1
            available.pop(tid, None)
            obj["track_id"] = tid
            self.previous[tid] = {"at": at, "centre": centre, "class_name": obj["class_name"]}
        self.previous = {
            key: value for key, value in self.previous.items() if at - value["at"] <= 1.1
        }
        return objects


class VideoProcessor:
    def __init__(self, path: Path, settings: Any, profile: dict[str, Any] | None = None):
        import cv2

        pinned = (
            validate_artifact(settings, profile)
            if profile is not None
            else pin_profile(settings, AnalysisRequest())
        )
        self.capture = cv2.VideoCapture(str(path))
        if not self.capture.isOpened():
            raise ValueError("Video decoder could not open the uploaded clip")
        if hasattr(cv2, "CAP_PROP_ORIENTATION_AUTO"):
            self.capture.set(cv2.CAP_PROP_ORIENTATION_AUTO, 0)
        self.detector = None
        self.background: Any = None
        self.tracker = RegionTracker()
        self.model = {
            "mode": "motion",
            "name": MOTION_NAME,
            "warning": MOTION_WARNING + (" " + pinned["warning"] if pinned.get("warning") else ""),
            "requested_mode": pinned["requested_mode"],
            "model_id": pinned["model_id"],
            "sample_fps": pinned["sample_fps"],
            "confidence_threshold": None,
        }
        if pinned["resolved_mode"] == "onnx":
            try:
                self.detector = load_pinned_detector(settings, pinned)
                self.model = {
                    "mode": "onnx",
                    "name": pinned["model_name"],
                    "weights_sha256": pinned["weights_sha256"],
                    "requested_mode": pinned["requested_mode"],
                    "model_id": pinned["model_id"],
                    "sample_fps": pinned["sample_fps"],
                    "confidence_threshold": pinned["confidence_threshold"],
                    "imgsz": pinned["imgsz"],
                    "warning": "Model detections and geometric region tracks require review; IDs do not establish personal identity.",
                }
            except Exception:
                self.capture.release()
                raise

    def sample(self, at: float, output: Path) -> list[dict[str, Any]] | None:
        import cv2

        self.capture.set(cv2.CAP_PROP_POS_MSEC, at * 1000)
        ok, image = self.capture.read()
        if not ok:
            return None
        height, width = image.shape[:2]
        if self.detector:
            objects = [
                {
                    "class_name": result.label,
                    "confidence": round(float(result.confidence), 4),
                    "bbox": [
                        float(result.bbox.x1) / width,
                        float(result.bbox.y1) / height,
                        float(result.bbox.x2) / width,
                        float(result.bbox.y2) / height,
                    ],
                }
                for result in self.detector.detect(image)[:32]
            ]
        else:
            small = cv2.resize(image, (320, max(16, round(height * 320 / width))))
            gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)
            objects = []
            if self.background is None:
                self.background = gray
            else:
                mask = cv2.threshold(
                    cv2.absdiff(self.background, gray), 25, 255, cv2.THRESH_BINARY
                )[1]
                mask = cv2.dilate(
                    mask, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)), iterations=2
                )
                contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                sh, sw = gray.shape
                for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:32]:
                    area = cv2.contourArea(contour) / (sw * sh)
                    if 0.002 <= area <= 0.6:
                        x, y, w, h = cv2.boundingRect(contour)
                        objects.append(
                            {
                                "class_name": "motion",
                                "confidence": None,
                                "bbox": [x / sw, y / sh, (x + w) / sw, (y + h) / sh],
                            }
                        )
        objects = self.tracker.update(objects, at)
        # Evidence uses the same full-frame privacy grid as playback, with annotations added after it.
        evidence_width = min(width, 640)
        evidence_height = max(16, round(height * evidence_width / width))
        mosaic = cv2.resize(
            cv2.resize(image, (16, 16), interpolation=cv2.INTER_AREA),
            (evidence_width, evidence_height),
            interpolation=cv2.INTER_NEAREST,
        )
        for obj in objects:
            x1, y1, x2, y2 = obj["bbox"]
            cv2.rectangle(
                mosaic,
                (int(x1 * evidence_width), int(y1 * evidence_height)),
                (int(x2 * evidence_width), int(y2 * evidence_height)),
                (0, 220, 255),
                2,
            )
            cv2.putText(
                mosaic,
                f"{obj['class_name']} {obj['track_id']}",
                (int(x1 * evidence_width), max(14, int(y1 * evidence_height) - 4)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.4,
                (0, 220, 255),
                1,
            )
        cv2.putText(
            mosaic,
            f"{at:.1f}s / pixelated review",
            (8, evidence_height - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (255, 255, 255),
            1,
        )
        if not cv2.imwrite(str(output), mosaic):
            raise ValueError("Evidence frame could not be saved")
        return objects

    def close(self):
        self.capture.release()
        if self.detector:
            self.detector.close()
