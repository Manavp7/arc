# tracking (M4)

Detections in, persistent identities out.

```
detections ──► tracking ──► tracks
                  │
                  ├─ ByteTracker (one per camera): Kalman + two-stage association + ReID rescue
                  ├─ CrossCameraAssociator: conservative same_as hypotheses between cameras
                  └─ HOTA evaluation harness
```

## Why ByteTrack, implemented here

BoxMOT's install path drags in PyTorch for what is ~250 lines of Kalman filtering and assignment, so
this is in-repo. `boxmot` and DeepStream MV3DT remain drop-in alternatives behind the same seam.

The idea worth preserving is that **low-confidence detections are not discarded**. Pass one matches
confident detections to all live tracks; pass two offers the *leftover tracks* the weak detections. A
partially occluded truck produces a weak detection, and a tracker that throws it away loses the
identity and invents a new one when the truck re-emerges — which is precisely what turns one truck's
visit into two, and makes dwell time and journey history worthless.

**Appearance is a tie-breaker, not the primary signal.** Geometry is the stronger frame-to-frame cue;
appearance alone confuses two identical white vans parked side by side. ReID earns its place on the
harder case — measured below.

The Kalman filter tracks `[cx, cy, aspect, height]` rather than four corners, so *motion* and
*apparent size* are modelled separately: a truck driving away moves smoothly and shrinks smoothly,
while a corner parameterisation couples the two and produces a filter that fights itself.

## Measured, on a deterministic synthetic sequence

3 trucks, 70 frames, object 0 occluded for 11 frames:

| scenario | HOTA | AssA | ID switches | ids created | ReID recoveries |
|---|---|---|---|---|---|
| straight-line occlusion | 0.980 | 1.000 | 0 | 3 | 0 |
| **turns while hidden**, with appearance | 0.973 | 1.000 | 0 | **3** | 1 |
| **turns while hidden**, IoU only | 0.906 | 0.867 | 1 | **4** | 0 |

The first row is why the second matters: a constant-velocity filter coasts through a straight-line
occlusion perfectly, so ReID is never asked and looks useless. Make the object *manoeuvre* while
hidden and the prediction lands somewhere else entirely — IoU is zero on re-emergence and appearance
is the only cue left. Without it the tracker invents a fourth identity for three objects.

## HOTA, not MOTA

HOTA = √(DetA · AssA) separates the two ways tracking fails. DetA asks whether the objects were found;
AssA asks whether the same id stayed on the same object. A tracker that finds everything and shuffles
ids scores well on MOTA and is useless here, because dwell time, "which camera last saw X" and
journey history all depend on the id, not the box. Implemented directly rather than depending on
TrackEval, which is a large dependency with its own data-format expectations.

## One tracker per camera, always

Track ids are identities in one camera's image space. Feeding two cameras' detections to one tracker
asks it to associate boxes that share no coordinate frame — and it will, producing tracks that
teleport across the site. Cross-camera identity is a different problem, solved separately.

Detections arrive per object, so the service batches them by `(source, frame)` and steps a tracker
only when a frame's worth has arrived or a 350 ms flush window expires. Stepping per detection would
advance the Kalman filter once per object and destroy the motion model.

## Cross-camera association is deliberately conservative

A link needs appearance similarity **and** a plausible transit time **and** the same class. The
failure modes are asymmetric: a missed link costs little (fusion may still merge on position), while a
wrong link fuses two vehicles into one entity whose journey history is fiction. Links are published as
`same_as` hypotheses with a confidence — fusion decides.

Tracks are also not offered for cross-camera matching until their appearance vector has settled
(5 hits), because an embedding smoothed over one or two frames is dominated by whichever crop came
first and matching on it produces confident nonsense.

## Endpoints

| | |
|---|---|
| `GET /tracks` | per-camera tracker state: ids, hits, age, path length, whether an embedding exists |
| `GET /cross-camera` | proposed links, rejection counts by reason |
| `GET /health` · `/metrics` | ops |
