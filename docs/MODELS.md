# Models and licences

Every model SIO uses, where it comes from, its licence, and what it costs on a laptop CPU.
Fetched by `just models` into `.sio/models/` with a SHA256 manifest in `infra/models.json`;
nothing here is committed to git.

## The headline: no PyTorch, no PaddlePaddle, no TensorFlow

Ultralytics publishes **pre-exported ONNX weights** alongside the PyTorch ones, and Hugging Face
hosts ONNX CLIP. So the entire perception stack runs on `onnxruntime` alone. Consequences:

- the default install is ~186 MB of models instead of several gigabytes of framework wheels
  (the vision stack is only 31 MB of that; CLIP's embedding tables are the rest);
- there is no CUDA/cuDNN version matrix to satisfy;
- the GPU swap is an execution-provider string (`CUDAExecutionProvider`, `TensorrtExecutionProvider`)
  rather than a different dependency tree.

## Detection, segmentation, re-identification

Source: `github.com/ultralytics/assets` release `v8.4.0`.

| File | Size | Input | Output | Measured CPU latency | Licence |
|---|---|---|---|---|---|
| `yolo26n.onnx` | 9.9 MB | `images [1,3,640,640]` | `output0 [1,300,6]` | **25 ms** at 640², 35-56 ms on a 1080p photo | AGPL-3.0 |
| `yolo26n-seg.onnx` | 11.2 MB | `[1,3,640,640]` | `[1,300,38]` + protos `[1,32,160,160]` | 85 ms | AGPL-3.0 |
| `yolo26n-reid.onnx` | 9.9 MB | `[batch,3,h,w]` dynamic | `embeddings [batch,512]` | **2 ms/crop** batched | AGPL-3.0 |

Measured on this project's development machine (x86_64, 8 vCPU, `intra_op_num_threads=2`) against
`ultralytics.com/images/bus.jpg` (810×1080). Verified output on that image: 1 bus and 4 people,
boxes tight to the objects — which is the real test of the letterbox inversion, since a wrong
inversion produces plausible-looking boxes that are consistently a few percent off.

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
| `Xenova/clip-vit-base-patch32` | `onnx/vision_model_int8.onnx` (88.6 MB), `onnx/text_model_int8.onnx` (64.1 MB), `tokenizer.json` (2.2 MB) | 155 MB | MIT (weights: OpenAI CLIP, MIT) |

Larger than the rest of the stack combined, and not much helped by quantisation: most of the bytes
are embedding tables. The int8 exports were chosen after comparing every variant on the hub — `q4`
is smaller for the vision tower but *larger* for the text tower (126 MB), which is the kind of thing
worth measuring rather than assuming.

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
