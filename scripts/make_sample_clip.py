#!/usr/bin/env python3
"""Build the sample media the perception pipeline runs against.

    just samples              # extract sprites and render a demo clip
    just samples --check      # report what exists
    just samples --clip-only  # re-render the clip from existing sprites

The problem this solves: the simulator knows *where* objects are, but a detector needs **pixels that
a detector can actually detect**. Coloured rectangles will not do — YOLO26 is trained on photographs,
and a synthetic blob produces either nothing or garbage, which makes the whole perception phase
untestable end to end.

So: take real photographs, use the **segmentation** model to cut objects out along their actual
silhouettes (not bounding boxes — a rectangular crop pastes a slab of Madrid pavement into a yard and
looks exactly as wrong as it sounds), and keep them as RGBA sprites. The simulator then composites
those sprites into each camera's view at the positions its own ground truth says they occupy. The
frames are synthetic; the objects in them are photographic, and YOLO26 detects them for the same
reason it detects them in the original photograph.

Deterministic: same seed, same sprites, same clip.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "libs" / "sio_core" / "src"))
sys.path.insert(0, str(REPO_ROOT / "libs" / "sio_schemas" / "src"))
sys.path.insert(0, str(REPO_ROOT / "services" / "perception" / "src"))
sys.path.insert(0, str(REPO_ROOT / "services" / "ingest" / "src"))

SOURCE_IMAGES = {
    "bus.jpg": "https://ultralytics.com/images/bus.jpg",
    "zidane.jpg": "https://ultralytics.com/images/zidane.jpg",
}

# Which detected classes are worth keeping as sprites, and what the yard calls them.
WANTED = {
    "bus": "truck",  # a bus body is the closest photographic stand-in for a box truck
    "truck": "truck",
    "car": "car",
    "person": "person",
}

MIN_SPRITE_PX = 60


@dataclass
class Sprite:
    """One cut-out object."""

    key: str
    label: str
    width: int
    height: int
    source: str
    confidence: float

    def as_json(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "width": self.width,
            "height": self.height,
            "source": self.source,
            "confidence": round(self.confidence, 3),
        }


def ensure_source_images(samples_dir: Path) -> list[Path]:
    """Download the source photographs if they are not already present."""
    paths: list[Path] = []
    samples_dir.mkdir(parents=True, exist_ok=True)
    for name, url in SOURCE_IMAGES.items():
        path = samples_dir / name
        if not path.exists():
            print(f"  fetching {name}")
            request = urllib.request.Request(url, headers={"User-Agent": "sio-make-samples/0.1"})
            with urllib.request.urlopen(request, timeout=60) as response:
                path.write_bytes(response.read())
        paths.append(path)
    return paths


def extract_sprites(
    images: list[Path], sprites_dir: Path, model_dir: Path, *, conf: float = 0.5
) -> list[Sprite]:
    """Cut objects out of photographs along their segmentation masks."""
    import cv2
    import numpy as np
    from sio_perception.detectors.onnx_yolo import OnnxYoloSegDetector, decode_rle

    seg_model = model_dir / "yolo26n-seg.onnx"
    if not seg_model.exists():
        raise SystemExit(f"segmentation model not found at {seg_model}\nrun: just models")

    detector = OnnxYoloSegDetector(seg_model, conf_threshold=conf, threads=2)
    sprites_dir.mkdir(parents=True, exist_ok=True)
    sprites: list[Sprite] = []

    for image_path in images:
        image = cv2.imread(str(image_path))
        if image is None:
            print(f"  ! could not read {image_path}")
            continue
        results = detector.detect(image)
        print(f"  {image_path.name}: {len(results)} objects")

        for index, result in enumerate(results):
            label = WANTED.get(result.label)
            if label is None or result.mask_rle is None:
                continue
            box = result.bbox
            if box.width < MIN_SPRITE_PX or box.height < MIN_SPRITE_PX:
                continue

            x1, y1 = int(box.x1), int(box.y1)
            x2, y2 = int(box.x2), int(box.y2)
            crop = image[y1:y2, x1:x2]
            if crop.size == 0:
                continue

            # The mask is in prototype resolution and cropped to the box, so resize it to the crop.
            mask = decode_rle(result.mask_rle).astype(np.uint8) * 255
            mask = cv2.resize(mask, (crop.shape[1], crop.shape[0]), interpolation=cv2.INTER_LINEAR)
            # Feather the edge: a hard mask edge leaves a bright halo when composited, and the
            # detector notices the halo before it notices the object.
            mask = cv2.GaussianBlur(mask, (5, 5), 0)

            rgba = np.dstack([crop, mask])
            key = f"{label}_{image_path.stem}_{index}.png"
            cv2.imwrite(str(sprites_dir / key), rgba)
            sprites.append(
                Sprite(
                    key=key,
                    label=label,
                    width=crop.shape[1],
                    height=crop.shape[0],
                    source=image_path.name,
                    confidence=result.confidence,
                )
            )
            print(
                f"    -> {key} ({crop.shape[1]}x{crop.shape[0]}, {result.label} {result.confidence:.2f})"
            )

    detector.close()
    return sprites


def verify_sprites_are_detectable(
    sprites_dir: Path, model_dir: Path, manifest: list[Sprite]
) -> int:
    """Composite each sprite onto a plausible background and check the detector still finds it.

    This is the step that makes the whole approach trustworthy. A sprite that the detector cannot
    find once composited is worse than useless: the pipeline would run, produce nothing, and the
    fault would look like a bug in perception rather than in the fixture.
    """
    import cv2
    from sio_perception.detectors.onnx_yolo import OnnxYoloDetector

    detector = OnnxYoloDetector(model_dir / "yolo26n.onnx", conf_threshold=0.3, threads=2)
    background = synthetic_yard_background(1280, 720)
    detectable = 0

    for sprite in manifest:
        rgba = cv2.imread(str(sprites_dir / sprite.key), cv2.IMREAD_UNCHANGED)
        if rgba is None or rgba.shape[2] != 4:
            continue
        frame = background.copy()
        # Scale to something a camera would plausibly see, and place it centrally.
        target_height = 320 if sprite.label != "person" else 260
        scale = target_height / rgba.shape[0]
        resized = cv2.resize(rgba, (max(8, int(rgba.shape[1] * scale)), target_height))
        composite(frame, resized, 640 - resized.shape[1] // 2, 400 - target_height // 2)

        found = [r.label for r in detector.detect(frame)]
        expected = {
            "truck": {"truck", "bus", "car"},
            "car": {"car", "truck"},
            "person": {"person"},
        }[sprite.label]
        ok = bool(expected & set(found))
        detectable += int(ok)
        marker = "ok " if ok else "!! "
        print(f"    {marker}{sprite.key:34} detector sees: {found or 'nothing'}")

    detector.close()
    return detectable


def synthetic_yard_background(width: int, height: int, *, seed: int = 7) -> object:
    """A plausible yard surface: asphalt with noise, lane markings and a horizon.

    Not decoration — texture matters. A flat grey field makes every composited object a
    high-contrast island, which flatters the detector and produces confidence numbers that do not
    survive contact with a real camera.
    """
    import cv2
    import numpy as np

    rng = np.random.default_rng(seed)
    frame = np.zeros((height, width, 3), dtype=np.uint8)

    # Sky-to-ground gradient.
    horizon = int(height * 0.33)
    for y in range(height):
        if y < horizon:
            shade = 150 - int(30 * (y / max(1, horizon)))
            frame[y, :] = (shade + 20, shade + 12, shade)
        else:
            depth = (y - horizon) / max(1, height - horizon)
            shade = 70 + int(28 * depth)
            frame[y, :] = (shade, shade + 2, shade + 4)

    # Asphalt grain.
    noise = rng.normal(0, 7, (height, width, 1)).astype(np.int16)
    frame = np.clip(frame.astype(np.int16) + noise, 0, 255).astype(np.uint8)

    # Perspective lane markings, so there is real structure for a detector to ignore.
    for lane in range(-2, 3):
        top_x = width // 2 + lane * 90
        bottom_x = width // 2 + lane * 300
        cv2.line(frame, (top_x, horizon), (bottom_x, height), (150, 150, 145), 3)

    # A dock-door row along the horizon.
    for index in range(6):
        x = 90 + index * 190
        cv2.rectangle(frame, (x, horizon - 60), (x + 130, horizon), (95, 95, 100), -1)
        cv2.rectangle(frame, (x, horizon - 60), (x + 130, horizon), (60, 60, 65), 2)

    return frame


def composite(frame: object, rgba: object, x: int, y: int) -> None:
    """Alpha-composite an RGBA sprite onto a BGR frame, clipped at the edges."""
    import numpy as np

    sprite_height, sprite_width = rgba.shape[:2]  # type: ignore[attr-defined]
    frame_height, frame_width = frame.shape[:2]  # type: ignore[attr-defined]

    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(frame_width, x + sprite_width), min(frame_height, y + sprite_height)
    if x2 <= x1 or y2 <= y1:
        return

    sprite_x1, sprite_y1 = x1 - x, y1 - y
    patch = rgba[sprite_y1 : sprite_y1 + (y2 - y1), sprite_x1 : sprite_x1 + (x2 - x1)]  # type: ignore[index]
    alpha = patch[:, :, 3:4].astype(np.float32) / 255.0
    region = frame[y1:y2, x1:x2]  # type: ignore[index]
    frame[y1:y2, x1:x2] = (patch[:, :, :3] * alpha + region * (1 - alpha)).astype(np.uint8)  # type: ignore[index]


def render_demo_clip(
    sprites_dir: Path, manifest: list[Sprite], out_dir: Path, *, frames: int = 48
) -> Path:
    """Render a short clip of objects crossing a camera view.

    Written as a JPEG sequence plus an MP4 when a codec is available. The sequence is the artifact
    that matters: it is what the perception tests read, and it needs no codec to exist.
    """
    import cv2
    import numpy as np

    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("frame_*.jpg"):
        stale.unlink()

    width, height = 1280, 720
    background = synthetic_yard_background(width, height)
    rng = np.random.default_rng(11)

    actors = []
    for sprite in manifest[:6]:
        rgba = cv2.imread(str(sprites_dir / sprite.key), cv2.IMREAD_UNCHANGED)
        if rgba is None:
            continue
        target_height = (
            int(rng.integers(200, 320)) if sprite.label != "person" else int(rng.integers(150, 240))
        )
        scale = target_height / rgba.shape[0]
        resized = cv2.resize(rgba, (max(8, int(rgba.shape[1] * scale)), target_height))
        actors.append(
            {
                "sprite": resized,
                "x": float(rng.integers(-200, width)),
                "y": float(height - target_height - int(rng.integers(10, 140))),
                "dx": float(rng.choice([-3.5, -2.0, 2.0, 3.5])),
                "label": sprite.label,
            }
        )

    written: list[Path] = []
    for index in range(frames):
        frame = background.copy()
        for actor in actors:
            actor["x"] += actor["dx"]
            if actor["x"] > width:
                actor["x"] = -actor["sprite"].shape[1]
            elif actor["x"] < -actor["sprite"].shape[1]:
                actor["x"] = float(width)
            composite(frame, actor["sprite"], int(actor["x"]), int(actor["y"]))
        path = out_dir / f"frame_{index:04d}.jpg"
        cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        written.append(path)

    # MP4 as a convenience for eyeballing; the JPEG sequence is the real artifact.
    try:
        video_path = out_dir.parent / "yard_cam.mp4"
        writer = cv2.VideoWriter(
            str(video_path), cv2.VideoWriter_fourcc(*"mp4v"), 12.0, (width, height)
        )
        if writer.isOpened():
            for path in written:
                writer.write(cv2.imread(str(path)))
            writer.release()
            print(f"  wrote {video_path.relative_to(REPO_ROOT)}")
    except Exception as exc:
        print(f"  (mp4 encode unavailable: {exc})")

    return out_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build SIO sample media")
    parser.add_argument("--check", action="store_true", help="report what exists")
    parser.add_argument("--clip-only", action="store_true", help="re-render the clip only")
    parser.add_argument("--frames", type=int, default=48, help="clip length in frames")
    parser.add_argument("--force", action="store_true", help="re-extract sprites")
    args = parser.parse_args(argv)

    from sio_core.config import get_settings

    cfg = get_settings()
    samples_dir = REPO_ROOT / cfg.samples_dir
    sprites_dir = samples_dir / "sprites"
    model_dir = REPO_ROOT / cfg.model_dir
    manifest_path = sprites_dir / "manifest.json"

    if args.check:
        sprites = list(sprites_dir.glob("*.png")) if sprites_dir.exists() else []
        clip = (
            list((samples_dir / "clip").glob("frame_*.jpg"))
            if (samples_dir / "clip").exists()
            else []
        )
        print(
            f"sprites: {len(sprites)}   clip frames: {len(clip)}   manifest: {manifest_path.exists()}"
        )
        return 0 if sprites and clip else 1

    print(f"samples directory: {samples_dir}")

    if args.clip_only and manifest_path.exists():
        manifest = [Sprite(**entry) for entry in json.loads(manifest_path.read_text())["sprites"]]
    else:
        if args.force and sprites_dir.exists():
            shutil.rmtree(sprites_dir)
        images = ensure_source_images(samples_dir)
        print("extracting sprites along segmentation masks")
        manifest = extract_sprites(images, sprites_dir, model_dir)
        if not manifest:
            print("no sprites extracted", file=sys.stderr)
            return 1
        manifest_path.write_text(
            json.dumps({"sprites": [sprite.as_json() for sprite in manifest]}, indent=2) + "\n"
        )

        print("verifying that the detector still finds each sprite once composited")
        detectable = verify_sprites_are_detectable(sprites_dir, model_dir, manifest)
        print(f"  {detectable}/{len(manifest)} sprites are detectable on a synthetic background")
        if detectable == 0:
            print(
                "no sprite survived compositing — the fixture would make perception look broken",
                file=sys.stderr,
            )
            return 1

    print(f"rendering a {args.frames}-frame demo clip")
    clip_dir = render_demo_clip(sprites_dir, manifest, samples_dir / "clip", frames=args.frames)
    count = len(list(clip_dir.glob("frame_*.jpg")))
    print(f"\n{len(manifest)} sprites, {count} clip frames in {samples_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
