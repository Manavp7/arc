# Recorded-video analysis profiles

Each new analysis can explicitly use motion or the server-configured ONNX detector. The API also retains `auto` for older clients: it resolves to a compatible installed ONNX detector or to motion, recording the actual choice and reason. Choosing a model never downloads weights, changes a live source, or changes saved recording zones and rules.

`GET /api/review/videos` includes `capabilities.analysis_profiles`: motion plus the configured detector's safe display name, stable `model_id`, availability, SHA-256 when present, and class labels from its embedded metadata when available. Compatibility uses an actual local detector load/warmup, cached by artifact hash and image size. Failed compatibility checks disable that ONNX choice. The server accepts only its configured detector, at most 256 MiB; no client filenames, model paths, external URLs, weight uploads or install actions are accepted. A self-contained ONNX file is required: untracked external-data sidecars are not loaded.

## Start a run

The local development installation was verified on 11 September 2026 with the official [YOLO26 Nano ONNX asset](https://github.com/ultralytics/assets/releases/tag/v8.4.0) at `.sio/models/yolo26n.onnx` (AGPL-3.0). Its SHA-256 is `2e947b787d9e787b93a16772a5f55b1d4d8c4d86f53146149c5d6a642442d6f7`, matching `infra/models.json`. This local binary is not bundled into other deployments by the guide.

In **Footage → Analysis controls**, select **yolo26n.onnx**, choose 1 or 2 samples per second and a confidence threshold, then **Run analysis**. For object rules, select an actual detector class such as `person`, `car` or `bus`; existing motion rules retain their `motion` target and do not automatically become object rules. The model advertises 80 classes. Live sources keep their separately configured detector.

`POST /api/review/videos/{video_id}/analyze` accepts an optional JSON body:

```json
{"mode":"motion","sample_fps":1,"confidence_threshold":0.35}
```

`mode` is `motion`, `onnx`, or `auto`; omitting the body preserves automatic selection. Sampling is exactly 1 or 2 fps, with a default of 2, and stays within the existing 180-second/360-sample limit. Confidence must be finite and between 0.05 and 0.95; its default is advertised in capabilities. It controls ONNX detections only. Motion regions have no confidence, and the actual model provenance reports a null threshold for motion. Unknown fields and unsupported values return 422.

Explicit ONNX requests fail when the configured detector is missing or incompatible. Once a run resolves to ONNX, a later load failure cannot switch it to motion, including automatic runs. A conflicting profile on an already active recording returns 409; the same active profile is reused.

## Persistence and provenance

The analysis's `profile` and the job's `configuration.profile` preserve requested and resolved mode, sampling rate, threshold, image size, model ID/name and optional weights hash. These values are fixed before a new job is queued. Actual `analysis.model` provenance includes the mode, threshold where applicable, sample fps and ONNX hash. Existing case/report snapshots and retained analysis versions remain unchanged.

Retries and restart recovery reuse the saved profile. The worker and explicit retry recheck the configured artifact's hash; changed or missing weights require restoring the exact pinned artifact or starting a separate new analysis. The detector loads a private temporary copy whose bytes match that hash. Replacing a configured file cannot silently change queued evidence. Restart recovery still creates a fresh analysis attempt and preserves interrupted attempts. Older queued jobs without profiles resolve once when first processed and retain that choice for later recovery; they cannot acquire a historical enqueue-time pin retroactively.

## Playback and limits

`video.fps` describes uploaded source media. Protected playback is generated at 15 fps and new uploads expose `playback_fps: 15`. Older records get their playback fps from a cached probe of the actual protected rendition; unavailable metadata stays unknown. Frame controls must use `playback_fps`, not source fps or detector sampling rate.

These controls do not establish accuracy. Motion is foreground change for fixed cameras; ONNX detections and region tracks require review. Evaluate candidate versions against separately frozen reviewer annotations. Privacy pixelation, audio removal, local processing limits, queue admission and explicit cancellation remain in effect.

`pytest tests/unit/test_analysis_profiles.py tests/unit/test_video_review.py tests/unit/test_video_jobs_storage.py` covers actual authored ONNX inference and thresholds, sampling, input bounds, no fallback, model replacement, retries/restarts, legacy queues and playback metadata. The tiny constant-output model is authored solely for regression tests and makes no detector performance claim.
