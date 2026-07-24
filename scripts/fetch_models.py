#!/usr/bin/env python3
"""Download the model set, verifying checksums.

    just models                    # required models (~180 MB)
    just models --all              # plus optional ones (audio, OCR)
    just models --check            # report what is present and whether it is intact
    just models --list             # show the registry with sizes and licences
    just models --update-manifest  # re-record checksums after a deliberate version change
    just models --llm              # also pull the pinned Ollama model

Everything lands in ``.sio/models`` and nothing is committed. ``infra/models.json`` holds the
expected SHA256 of each file, so a truncated download or a silently re-published asset is caught
here rather than surfacing later as nonsense detections.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "infra" / "models.json"

sys.path.insert(0, str(REPO_ROOT / "libs" / "sio_core" / "src"))
sys.path.insert(0, str(REPO_ROOT / "libs" / "sio_schemas" / "src"))

ULTRALYTICS_RELEASE = "https://github.com/ultralytics/assets/releases/download/v8.4.0"
CLIP_REPO = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main"
AST_REPO = (
    "https://huggingface.co/onnx-community/ast-finetuned-audioset-10-10-0.4593-ONNX/resolve/main"
)


@dataclass(frozen=True)
class ModelAsset:
    """One downloadable file."""

    key: str
    filename: str
    url: str
    purpose: str
    licence: str
    approx_mb: float
    optional: bool = False

    @property
    def dest(self) -> Path:
        return Path(filename)


ASSETS: tuple[ModelAsset, ...] = (
    ModelAsset(
        key="detect",
        filename="yolo26n.onnx",
        url=f"{ULTRALYTICS_RELEASE}/yolo26n.onnx",
        purpose="Object detection. End-to-end head: output [1,300,6] is x1,y1,x2,y2,conf,cls "
        "with no NMS required. ~25 ms/frame on a laptop CPU.",
        licence="AGPL-3.0 (Ultralytics)",
        approx_mb=9.9,
    ),
    ModelAsset(
        key="segment",
        filename="yolo26n-seg.onnx",
        url=f"{ULTRALYTICS_RELEASE}/yolo26n-seg.onnx",
        purpose="Instance segmentation: [1,300,38] plus 32 mask prototypes at 160x160. "
        "Used instead of SAM 3.1 on CPU (PRD open question Q3).",
        licence="AGPL-3.0 (Ultralytics)",
        approx_mb=11.2,
    ),
    ModelAsset(
        key="reid",
        filename="yolo26n-reid.onnx",
        url=f"{ULTRALYTICS_RELEASE}/yolo26n-reid.onnx",
        purpose="512-d appearance embeddings for re-identification: occlusion recovery and "
        "cross-camera association.",
        licence="AGPL-3.0 (Ultralytics)",
        approx_mb=9.9,
    ),
    ModelAsset(
        key="clip_vision",
        filename="clip-vision.onnx",
        url=f"{CLIP_REPO}/onnx/vision_model_int8.onnx",
        purpose="CLIP image encoder (int8). Embeds frames into the same 512-d space as the text "
        "encoder, which is what makes 'red truck at the gate' searchable.",
        licence="MIT (OpenAI CLIP weights, Xenova ONNX export)",
        approx_mb=88.6,
    ),
    ModelAsset(
        key="clip_text",
        filename="clip-text.onnx",
        url=f"{CLIP_REPO}/onnx/text_model_int8.onnx",
        purpose="CLIP text encoder (int8). Turns a natural-language query into a 512-d vector.",
        licence="MIT (OpenAI CLIP weights, Xenova ONNX export)",
        approx_mb=64.1,
    ),
    ModelAsset(
        key="clip_tokenizer",
        filename="clip-tokenizer.json",
        url=f"{CLIP_REPO}/tokenizer.json",
        purpose="CLIP tokeniser, read by the `tokenizers` package. No transformers, no torch.",
        licence="MIT",
        approx_mb=2.2,
    ),
    ModelAsset(
        key="audio_ast",
        filename="ast-audioset.onnx",
        url=f"{AST_REPO}/onnx/model_quantized.onnx",
        purpose="AudioSet sound-event detection (527 classes: gunshot, glass, scream, explosion). "
        "Enable with SIO_ENABLE_AUDIO=true.",
        licence="MIT (model), CC-BY (AudioSet)",
        approx_mb=88.0,
        optional=True,
    ),
)

CHUNK = 1 << 16


def human(mb: float) -> str:
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest() -> dict[str, dict[str, object]]:
    if not MANIFEST_PATH.exists():
        return {}
    data = json.loads(MANIFEST_PATH.read_text())
    return data.get("models", {})


def save_manifest(entries: dict[str, dict[str, object]]) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(
            {
                "comment": (
                    "Expected SHA256 for every model file fetched by scripts/fetch_models.py. "
                    "A mismatch means a truncated download or a re-published asset; either way the "
                    "model is not the one this code was written against."
                ),
                "models": dict(sorted(entries.items())),
            },
            indent=2,
        )
        + "\n"
    )


def download(asset: ModelAsset, dest: Path, *, quiet: bool = False) -> None:
    """Stream to a temporary file, then move into place.

    Downloading straight to the destination means an interrupted transfer leaves a half-file that
    looks present — and a truncated ONNX model fails at inference time with something unhelpful.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_suffix(dest.suffix + ".part")
    request = urllib.request.Request(asset.url, headers={"User-Agent": "sio-fetch-models/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            total = int(response.headers.get("content-length") or 0)
            written = 0
            with temporary.open("wb") as handle:
                while chunk := response.read(CHUNK):
                    handle.write(chunk)
                    written += len(chunk)
                    if not quiet and total:
                        percent = written * 100 // total
                        print(
                            f"\r  {asset.filename:24} {percent:3d}%  "
                            f"{written / 1e6:6.1f} / {total / 1e6:.1f} MB",
                            end="",
                            flush=True,
                        )
        if not quiet:
            print()
    except (urllib.error.URLError, TimeoutError) as exc:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"download failed for {asset.filename}: {exc}") from exc
    temporary.replace(dest)


def pull_llm() -> int:
    """Pull the pinned Ollama model, if Ollama is installed."""
    from sio_core.config import get_settings

    model = get_settings().llm_model
    if shutil.which("ollama") is None:
        print(
            f"  ollama not installed; skipping {model} (copilot can use SIO_LLM_PROVIDER=scripted)"
        )
        return 0
    print(f"  pulling {model} (this can take a few minutes)")
    result = subprocess.run(["ollama", "pull", model], check=False)
    if result.returncode != 0:
        print(f"  ollama pull {model} failed; is the server running? (just services ollama)")
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch SIO model weights")
    parser.add_argument("--all", action="store_true", help="include optional models")
    parser.add_argument(
        "--check", action="store_true", help="verify what is present, download nothing"
    )
    parser.add_argument("--list", action="store_true", help="print the registry and exit")
    parser.add_argument("--update-manifest", action="store_true", help="re-record checksums")
    parser.add_argument("--llm", action="store_true", help="also pull the pinned Ollama model")
    parser.add_argument("--force", action="store_true", help="re-download even if present")
    args = parser.parse_args(argv)

    if args.list:
        print(f"{'key':16} {'file':26} {'size':>8}  licence")
        for asset in ASSETS:
            flag = " (optional)" if asset.optional else ""
            print(
                f"{asset.key:16} {asset.filename:26} {human(asset.approx_mb):>8}  {asset.licence}{flag}"
            )
        required = sum(a.approx_mb for a in ASSETS if not a.optional)
        print(
            f"\nrequired total: {human(required)}; with optional: {human(sum(a.approx_mb for a in ASSETS))}"
        )
        return 0

    from sio_core.config import get_settings

    cfg = get_settings()
    model_dir = REPO_ROOT / cfg.model_dir
    model_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()
    wanted = [asset for asset in ASSETS if args.all or not asset.optional]

    print(f"models directory: {model_dir}")
    failures = 0
    entries: dict[str, dict[str, object]] = dict(manifest)

    for asset in wanted:
        path = model_dir / asset.filename
        expected = str(manifest.get(asset.filename, {}).get("sha256", "")) or None

        if args.check:
            if not path.exists():
                print(f"  {asset.filename:26} MISSING")
                failures += 1
                continue
            digest = sha256_of(path)
            if expected and digest != expected:
                print(
                    f"  {asset.filename:26} CORRUPT (sha256 {digest[:12]}, expected {expected[:12]})"
                )
                failures += 1
            else:
                print(f"  {asset.filename:26} ok ({path.stat().st_size / 1e6:.1f} MB)")
            continue

        if path.exists() and not args.force:
            digest = sha256_of(path)
            if expected is None or digest == expected:
                print(f"  {asset.filename:26} present")
                entries.setdefault(
                    asset.filename,
                    {"sha256": digest, "bytes": path.stat().st_size, "url": asset.url},
                )
                continue
            print(f"  {asset.filename:26} checksum mismatch; re-downloading")

        try:
            download(asset, path)
        except RuntimeError as exc:
            print(f"  {exc}", file=sys.stderr)
            failures += 1
            continue

        digest = sha256_of(path)
        if expected and digest != expected and not args.update_manifest:
            print(
                f"  {asset.filename:26} CHECKSUM MISMATCH after download\n"
                f"    expected {expected}\n    actual   {digest}\n"
                "    the upstream asset may have been re-published; if that is expected, re-run "
                "with --update-manifest",
                file=sys.stderr,
            )
            failures += 1
            continue
        entries[asset.filename] = {
            "sha256": digest,
            "bytes": path.stat().st_size,
            "url": asset.url,
            "licence": asset.licence,
        }

    if args.update_manifest:
        save_manifest(entries)
        print(f"manifest written: {MANIFEST_PATH.relative_to(REPO_ROOT)} ({len(entries)} files)")
    elif not manifest and not args.check:
        save_manifest(entries)
        print(f"manifest created: {MANIFEST_PATH.relative_to(REPO_ROOT)}")

    if args.llm:
        pull_llm()

    if failures:
        print(f"\n{failures} model(s) missing or corrupt", file=sys.stderr)
        return 1

    present = sum(
        (model_dir / asset.filename).stat().st_size
        for asset in wanted
        if (model_dir / asset.filename).exists()
    )
    print(f"\nall models ready ({present / 1e6:.0f} MB in {model_dir})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
