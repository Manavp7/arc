# fusion (M5)

N observations of one object become one entity. This is the component that makes the world model a
*world* model rather than a list of sightings.

```
tracks   ──┐
raw.gps  ──┼──► fusion ──► entities  (+ seen_by relationships)
raw.iot  ──┘
```

A truck at the gate is seen by a camera, carries a GPS tracker and trips an RFID reader. Without
fusion those are three unrelated records and "how long has that truck been on site" has no answer.

## A device id is identity; a camera detection is not

The two association paths are deliberately asymmetric, because the problems are not the same:

- **GPS and RFID carry a device id.** Fixes from the same tracker are the same object — that is what a
  device id *means*, and every fleet system relies on it. No positional gating is applied, because a
  tracker that has moved 500 m since its last fix is still the same tracker; gating it would split one
  vehicle into many whenever a fix was delayed.
- **A camera track has no such luxury.** Associating it takes position, time and class agreement, and
  appearance only as a last resort — two identical white vans look the same, so appearance may break a
  tie that position could not, never override position.

**What is deliberately not used:** the simulator puts `agent_id` and its own labels in every payload.
Associating on those would make fusion look flawless while testing nothing, so they are filtered out
before they can reach an entity, and a test asserts that they never appear on one.

## Gating on sigma, not on a radius

Association uses Mahalanobis distance against the filter's current covariance, so a freshly-created,
uncertain entity accepts a fix that a well-established one rejects. One fixed metre-radius cannot
serve both a stationary forklift and a 20 m/s drone — picking a number means picking it wrong for one
of them. A metre cap is still applied as a backstop.

## The filter runs in metres, and advances on measurement time

Two decisions that both started as bugs:

**Metres, not degrees.** A Kalman filter over latitude and longitude has a covariance whose axes carry
different units and whose scale varies with latitude, quietly invalidating every gate and noise term.
Local east/north metres relative to a site origin (derived from the zone geometry, not hard-coded)
removes that exactly at site scale.

**Measurement timestamps, not the wall clock.** The first version stepped the filter by
`time.monotonic()` deltas. With fixes a second apart in their own timestamps but microseconds apart in
arrival, `dt` was ~0, the filter never propagated, and a truck crossing the yard at 4 m/s was
estimated as stationary. It also breaks under replay and under at-least-once redelivery, where arrival
order and measurement order are different things. Long gaps are capped at 30 s, because propagating a
stale velocity for minutes puts the estimate somewhere fictional.

**A Kalman filter, not an EKF.** The PRD says EKF; for a position measurement of a constant-velocity
object the model is *linear*, and an EKF would contribute jacobians that are identity matrices. The
non-linear part — geodetic to local — is handled once, exactly, by the projection.

## Camera calibration is data, not code

`GroundProjector` places an image-space box on the ground using the camera's pose, read from the
`sources` table. Bearing comes from the box's horizontal position within the field of view; range from
the vertical position of the box's **bottom edge** (the only part that reliably touches the ground)
under a flat-ground assumption.

Both assumptions are stated rather than hidden, because they decide when the output can be trusted:
flat ground within a yard is reasonable, a sloped approach road is not. `position_sigma_m` grows with
range — roughly with its square — so a 50 m detection is weighted far below a GPS fix instead of
dragging an entity off its track. Boxes above the horizon are rejected outright: there is no ground
intersection, so there is no honest answer.

A fusion service that imported the simulator's site model to learn where the cameras are could never
run on a real site. Real deployments replace this with a surveyed homography or a calibration tool;
the projector interface is that seam.

## What it publishes

- **`Entity`** per fused object, at a fixed 1 Hz cadence rather than per observation — the state is a
  filtered *estimate* that changes continuously, not only when a message arrives — carrying position,
  velocity, heading, covariance, and provenance from every contributing sensor.
- **`seen_by` relationships** to each camera that contributed, opened once and left open. Because the
  world model's edges are bitemporal, "which camera last saw entity X" (UC3) stays answerable long
  after the entity has left.

An entity is only published once it has at least two observations. A single sighting is not an object,
and a ghost with a plausible position is harder to spot than no entity at all.

## Endpoints

| | |
|---|---|
| `GET /fusion` | association statistics: matches by device, track, position, appearance; gate rejections |
| `GET /fusion/entities` | fused entities with the provenance behind each |
| `GET /health` · `/metrics` | ops |
