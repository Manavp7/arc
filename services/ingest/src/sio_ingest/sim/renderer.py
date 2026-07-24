"""Render camera frames for the simulated yard.

The simulator knows where every object is; a detector needs pixels it can actually detect. So each
camera view is composited from **photographic sprites** — real objects cut out along their
segmentation masks by ``scripts/make_sample_clip.py`` — placed at exactly the bounding boxes the
simulator's own ground truth specifies.

That last detail is what makes the whole thing worth doing: because a sprite is resized to the
ground-truth box, a detection from the real model can be compared against the ground truth box for
the same object. The detection eval harness gets true positives, false positives and localisation
error for free, from a scene nobody had to label.

Degrades honestly: with no sprites available, :meth:`CameraRenderer.render` returns ``None``, the
ingest service publishes the observation without a ``raw_ref``, and perception falls back to the
synthetic detector. No silent black frames.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sio_core import get_logger

log = get_logger("sio.ingest.renderer")

FRAME_WIDTH = 1280
FRAME_HEIGHT = 720
JPEG_QUALITY = 85


class CameraRenderer:
    """Composites photographic sprites into a synthetic camera view.

    One instance serves every camera. Backgrounds are generated per camera id and cached, so each
    camera has a stable, distinguishable scene — a camera whose background changed every frame would
    make the fire detector's flicker measure meaningless.
    """

    def __init__(self, samples_dir: Path | str, *, enabled: bool = True) -> None:
        self.samples_dir = Path(samples_dir)
        self.sprites_dir = self.samples_dir / "sprites"
        self.enabled = enabled
        self._sprites: dict[str, list[Any]] = {}
        self._backgrounds: dict[str, Any] = {}
        self._loaded = False
        self._frames_rendered = 0
        self._load_failures = 0

    # ------------------------------------------------------------------ loading
    def load(self) -> bool:
        """Load the sprite library. Returns False when rendering is not possible."""
        if self._loaded:
            return bool(self._sprites)
        self._loaded = True
        if not self.enabled:
            return False

        manifest_path = self.sprites_dir / "manifest.json"
        if not manifest_path.exists():
            log.warning(
                "renderer.no_sprites",
                looked_for=str(manifest_path),
                effect="frames will not be rendered; perception will use the synthetic detector",
                hint="run: just samples",
            )
            return False
        try:
            import cv2
        except ImportError:
            log.warning("renderer.no_opencv", effect="frames will not be rendered")
            return False

        manifest = json.loads(manifest_path.read_text())
        for entry in manifest.get("sprites", []):
            path = self.sprites_dir / entry["key"]
            rgba = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
            if rgba is None or rgba.shape[2] != 4:
                self._load_failures += 1
                continue
            self._sprites.setdefault(entry["label"], []).append(rgba)

        log.info(
            "renderer.loaded",
            classes={label: len(items) for label, items in self._sprites.items()},
            failures=self._load_failures,
        )
        return bool(self._sprites)

    @property
    def available(self) -> bool:
        return self.load()

    # ---------------------------------------------------------------- rendering
    def render(self, source_id: str, payload: dict[str, Any]) -> bytes | None:
        """Render one frame from a frame observation's payload, as JPEG bytes."""
        if not self.available:
            return None
        import cv2
        import numpy as np

        frame = self._background(source_id).copy()
        placed = 0

        for item in payload.get("visible", []):
            box = item.get("bbox")
            sprite = self._sprite_for(str(item.get("class", "")), item.get("agent_id", ""))
            if not box or sprite is None:
                continue
            x1, y1, x2, y2 = (float(value) for value in box)
            width, height = int(x2 - x1), int(y2 - y1)
            if width < 8 or height < 8:
                continue
            resized = cv2.resize(sprite, (width, height), interpolation=cv2.INTER_AREA)
            _composite(frame, resized, int(x1), int(y1))
            placed += 1

        if payload.get("fire"):
            _draw_fire(frame, self._frames_rendered)

        if placed == 0 and not payload.get("fire"):
            return None  # nothing to see: do not spend bytes on an empty frame

        success, encoded = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
        )
        if not success:
            return None
        self._frames_rendered += 1
        return bytes(encoded.tobytes())

    def _sprite_for(self, detector_class: str, agent_id: str) -> Any | None:
        """Pick a sprite for a class, stable per agent.

        Stable on purpose: if a truck's appearance changed every frame, the ReID embeddings would be
        uncorrelated between frames and the tracker's appearance matching would be exercised against
        noise rather than against a consistent object.
        """
        candidates = self._sprites.get(detector_class)
        if not candidates:
            # A forklift is detected as a truck; a drone has no photographic stand-in.
            candidates = self._sprites.get("truck") if detector_class in ("truck", "car") else None
        if not candidates:
            return None
        index = (hash(agent_id) if agent_id else 0) % len(candidates)
        return candidates[index]

    def _background(self, source_id: str) -> Any:
        if source_id not in self._backgrounds:
            self._backgrounds[source_id] = _synthetic_background(
                FRAME_WIDTH, FRAME_HEIGHT, seed=abs(hash(source_id)) % 10_000
            )
        return self._backgrounds[source_id]

    def stats(self) -> dict[str, Any]:
        return {
            "available": bool(self._sprites),
            "sprite_classes": {label: len(items) for label, items in self._sprites.items()},
            "frames_rendered": self._frames_rendered,
        }


def _composite(frame: Any, rgba: Any, x: int, y: int) -> None:
    """Alpha-composite an RGBA sprite onto a BGR frame, clipped at the frame edges."""
    import numpy as np

    sprite_height, sprite_width = rgba.shape[:2]
    frame_height, frame_width = frame.shape[:2]
    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(frame_width, x + sprite_width), min(frame_height, y + sprite_height)
    if x2 <= x1 or y2 <= y1:
        return
    patch = rgba[y1 - y : y1 - y + (y2 - y1), x1 - x : x1 - x + (x2 - x1)]
    alpha = patch[:, :, 3:4].astype(np.float32) / 255.0
    region = frame[y1:y2, x1:x2]
    frame[y1:y2, x1:x2] = (patch[:, :, :3] * alpha + region * (1 - alpha)).astype(np.uint8)


def _synthetic_background(width: int, height: int, *, seed: int) -> Any:
    """Asphalt, lane markings, a dock-door row and grain.

    Texture is not decoration: a flat grey field makes every composited object a high-contrast
    island, which flatters the detector and produces confidence numbers that do not survive contact
    with a real camera.
    """
    import cv2
    import numpy as np

    rng = np.random.default_rng(seed)
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    horizon = int(height * 0.33)

    for y in range(height):
        if y < horizon:
            shade = 148 - int(28 * (y / max(1, horizon)))
            frame[y, :] = (shade + 18, shade + 10, shade)
        else:
            depth = (y - horizon) / max(1, height - horizon)
            shade = 68 + int(30 * depth)
            frame[y, :] = (shade, shade + 2, shade + 4)

    noise = rng.normal(0, 6, (height, width, 1)).astype(np.int16)
    frame = np.clip(frame.astype(np.int16) + noise, 0, 255).astype(np.uint8)

    for lane in range(-2, 3):
        cv2.line(
            frame,
            (width // 2 + lane * 85, horizon),
            (width // 2 + lane * 290, height),
            (148, 148, 143),
            3,
        )
    for index in range(6):
        x = 80 + index * 195
        cv2.rectangle(frame, (x, horizon - 58), (x + 128, horizon), (94, 94, 99), -1)
        cv2.rectangle(frame, (x, horizon - 58), (x + 128, horizon), (58, 58, 63), 2)
    return frame


def _draw_fire(frame: Any, tick: int) -> None:
    """Draw a flickering flame region.

    Irregular and time-varying on purpose: the fire heuristic requires shape irregularity *and*
    frame-to-frame change, so a smooth static blob would correctly not be detected — and the demo
    would show a fire that the platform ignores.
    """
    import cv2
    import numpy as np

    rng = np.random.default_rng(tick)
    centre_x, centre_y = FRAME_WIDTH // 2, int(FRAME_HEIGHT * 0.62)
    for _ in range(80):
        x = int(centre_x + rng.normal(0, 55))
        y = int(centre_y + rng.normal(0, 42))
        radius = int(rng.integers(6, 30))
        # BGR: deep red through orange to yellow.
        colour = (
            int(rng.integers(10, 60)),
            int(rng.integers(90, 190)),
            int(rng.integers(225, 255)),
        )
        cv2.circle(frame, (x, y), radius, colour, -1)
    # Smoke above the flame.
    for _ in range(40):
        x = int(centre_x + rng.normal(0, 70))
        y = int(centre_y - 90 + rng.normal(0, 60))
        radius = int(rng.integers(20, 55))
        grey = int(rng.integers(120, 190))
        overlay = frame.copy()
        cv2.circle(overlay, (x, y), radius, (grey, grey, grey), -1)
        cv2.addWeighted(overlay, 0.25, frame, 0.75, 0, frame)
