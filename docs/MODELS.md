# Models and licences

Every model SIO uses, where it comes from, its licence, and what it costs on a laptop CPU.
Fetched by `just models` into `.sio/models/` with a SHA256 manifest in `infra/models.json`;
nothing here is committed to git.

## The headline: no PyTorch, no PaddlePaddle, no TensorFlow

Ultralytics publishes **pre-exported ONNX weights** alongside the PyTorch ones, and Hugging Face
hosts ONNX CLIP. So the entire perception stack runs on `onnxruntime` alone. Consequences:

- the default install is ~45 MB of models instead of several gigabytes of framework wheels;
- there is no CUDA/cuDNN version matrix to satisfy;
- the GPU swap is an execution-provider string (`CUDAExecutionProvider`, `TensorrtExecutionProvider`)
  rather than a different dependency tree.

## Detection, segmentation, re-identification

Source: `github.com/ultralytics/assets` release `v8.4.0`.

| File | Size | Input | Output | Measured CPU latency | Licence |
|---|---|---|---|---|---|
| `yolo26n.onnx` | 9.9 MB | `images [1,3,640,640]` | `output0 [1,300,6]` | **25 ms/frame** | AGPL-3.0 |
| `yolo26n-seg.onnx` | 11.2 MB | `[1,3,640,640]` | `[1,300,38]` + protos `[1,32,160,160]` | ~55 ms/frame | AGPL-3.0 |
| `yolo26n-reid.onnx` | 9.9 MB | `[batch,3,h,w]` dynamic | `embeddings [batch,512]` | ~8 ms/crop | AGPL-3.0 |

Latency measured on this project's development machine (x86_64, 8 vCPU, `intra_op_num_threads=2`).

**YOLO26's default head is end-to-end / NMS-free.** `output0` rows are
`[x1, y1, x2, y2, confidence, class]` in letterboxed 640-space, sorted by descending confidence,
capped at 300 detections. Post-processing is therefore a confidence threshold plus inverting the
letterbox — no NMS implementation, no anchor decoding, no DFL. Class names travel inside the ONNX
file (`custom_metadata_map['names']`), so there is no labels file to keep in sync.

> **Licence note:** Ultralytics weights are AGPL-3.0. Fine for this repository and for internal
> use; a commercial closed-source deployment needs an Ultralytics Enterprise licence or a
> permissively licensed substitute. The `Detector` port exists partly so that substitution is a
> configuration change — RT-DETR (Apache-2.0) and DeepStream models drop into the same seam.

## Semantic search (text ↔ image)

| Model | Files | Size | Licence |
|---|---|---|---|
| `Xenova/clip-vit-base-patch32` | `onnx/vision_model_quantized.onnx`, `onnx/text_model_quantized.onnx`, `tokenizer.json` | ~40 MB total | MIT (weights: OpenAI CLIP, MIT) |

Gives 512-d embeddings for both frames and text queries, so "red truck at the gate" searches the
frame index directly. Tokenisation uses the `tokenizers` package — no transformers, no torch.
512 dimensions matches the ReID output, so one `vector(512)` column serves both.

## OCR (plates, container IDs)

RapidOCR (`rapidocr`, ONNX-based, Apache-2.0) rather than PaddleOCR. Same accuracy class for
this task, no PaddlePaddle dependency. Detection + recognition models download on first use
(~15 MB).

## Audio event detection — optional, off by default

`onnx-community/ast-finetuned-audioset-10-10-0.4593-ONNX` (AudioSet AST, 527 classes including
gunshot, glass breaking, scream, explosion). Enabled with `SIO_ENABLE_AUDIO=true`. Chosen over
PANNs and YAMNet because both require torch or TensorFlow. Licence: MIT (model), CC-BY (AudioSet).

## Fire and smoke — an honest limitation

There is no COCO class for fire, and no ONNX fire detector we can rely on being available and
permissively licensed. The MVP therefore uses `FireHeuristicDetector`: HSV colour thresholding
plus temporal flicker energy and area growth, fused with thermal IoT readings. It is a
**documented stand-in**, not a model, and it is labelled as such in every explanation it
contributes to (`evidence[].note`).

The upgrade path is a fine-tune of `yolo26n` on a fire/smoke dataset (D-Fire, FASDD), exported to
ONNX, dropped in as `SIO_DET_MODEL`. No code change.

## Local LLM (copilot)

Pulled by `just models` via Ollama; the exact tag is pinned in `.env.example` (never `:latest`).

**Selection is a tool-calling decision, not a fluency one.** `scripts/eval_tool_calling.py` scores
candidate models on a 25-prompt fixture measuring correct tool selection, well-formed JSON
arguments and multi-tool ordering; a model must reach ≥ 90 % before it can be the default,
because a model that chats well but cannot pick among nine tools presents to a user as a broken
copilot. Results are recorded here when Phase 4 lands.

GPU/production swap: `SIO_LLM_PROVIDER=openai_compat` with `SIO_OPENAI_BASE_URL` pointing at
vLLM, SGLang or NIM serving Nemotron 3.

## Forecasting

StatsForecast (Apache-2.0) AutoETS/AutoARIMA on CPU; no downloaded weights. `TimesFMForecaster`
and Moirai-2 are Phase 7 adapters behind the same `Forecaster` port.
