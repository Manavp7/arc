# perception (M3)

Frames in, structured detections out. Pure ONNX Runtime: no PyTorch, no PaddlePaddle, no TensorFlow.

```
raw.frames ──► perception ──► detections
                  │
                  ├─ yolo26n.onnx        detection      (25 ms @ 640², end-to-end head)
                  ├─ yolo26n-seg.onnx    segmentation   (optional, SIO_ENABLE_SEGMENTATION)
                  ├─ yolo26n-reid.onnx   512-d appearance vectors for tracking
                  ├─ fire heuristic      colour + flicker + shape + growth
                  └─ redaction           faces and plates blurred BEFORE storage
```

## The decode is short, and that is the interesting part

YOLO26's default head is end-to-end. `output0` is `[1, 300, 6]` — `x1, y1, x2, y2, confidence,
class` — already sorted by confidence and already de-duplicated. So `_postprocess` is: threshold,
map the class index through the names embedded in the ONNX metadata, invert the letterbox. No NMS, no
anchor decoding, no DFL integration.

The letterbox inversion is the part that needed care. Getting it subtly wrong produces boxes that
look plausible and are consistently a few percent off, so it is an explicit object with an
`invert()`, and it is verified against real photographs rather than synthetic tensors.

## Detector selection

| `SIO_DETECTOR` | What runs |
|---|---|
| `auto` (default) | real weights if present, else `synthetic` **with a warning** |
| `onnx` | YOLO26 detection |
| `onnx_seg` | YOLO26 segmentation (boxes + RLE masks) |
| `synthetic` | the simulator's ground truth, jittered and randomly dropped — for CI and for pipeline work |
| `null` | nothing, for measuring the cost of everything else |
| `deepstream` | Phase 7; raises with the CUDA-provider alternative rather than silently finding nothing |

GPU inference needs no different detector: `SIO_ORT_PROVIDERS=CUDAExecutionProvider` runs the same
`.onnx` files.

## Fire detection is a heuristic, and says so

There is no COCO fire class and no permissively-licensed ONNX fire model we can depend on. So fire
uses three signals, and **no single one can raise an alarm**:

- **colour** — the HSV band fire occupies (which a red truck also occupies);
- **flicker** — frame-to-frame change in the bright region;
- **shape irregularity** — concavity and perimeter complexity, which is *translation-invariant*.

That last one exists because of a false positive found while testing: a **stationary** red truck was
caught by requiring flicker, but a **driving** red truck still fooled it — a translating rectangle
produces a large frame difference while its outline stays a rectangle. Shape irregularity separates a
ragged flame from a rigid blob regardless of motion. Measured on synthetic cases: 0/10 false
positives for a static red truck, 0/10 for a moving one, 9/10 true positives on flame.

Every fire detection carries its component scores in `attrs` and is marked `heuristic: true`, so an
explanation shows an operator why. `ThermalFireCorroborator` raises confidence when a thermal sensor
in the same zone agrees, capped at 1.6× — corroboration should turn a maybe into a probably, not
manufacture certainty.

The upgrade path is a fine-tune of `yolo26n` on D-Fire or FASDD exported to ONNX, dropped in as
`SIO_DET_MODEL`. No code change.

## Redaction happens on the way in

Faces (top of a person box) and plates (lower-centre of a vehicle box) are Gaussian-blurred *and*
pixelated before the frame is written to the object store, because Gaussian blur alone is partially
invertible for small regions. Generous regions on purpose: too large costs nothing, too small costs
everything. This is not face recognition and cannot become it — no identity matching exists here, and
recognition stays off behind `SIO_ENABLE_FACE_RECOGNITION`.

## Two throughput decisions

**Inference runs in a worker thread.** A 25-50 ms forward pass on the event loop stalls bus
consumption for that whole time; at 2 fps across eight cameras that is a third of the loop blocked.

**Frames are sampled per camera, not globally.** `SIO_PERCEPTION_FPS` caps each camera
independently, because a global cap lets a busy camera starve a quiet one — and the quiet one is
often watching the gate nobody uses, which is exactly where an intrusion happens.

## Endpoints

| | |
|---|---|
| `GET /detector` | which model is running, providers, throughput, redaction flags |
| `POST /detect/sample?key=...` | run the detector over one stored frame |
| `GET /health` · `/metrics` | ops; `info` carries frame and detection counters |
