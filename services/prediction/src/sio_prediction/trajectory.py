"""Where will it be, and which zone will it be in (PRD M10, UC4).

Extrapolating a position is the easy half. The half that decides whether anyone can use the answer is
**saying how wrong it might be**, and being willing to say "nowhere in particular".

Three properties this gets right, each of which is a way trajectory prediction usually goes wrong:

* **Uncertainty is a cone, not a line.** Position error grows with the horizon — from velocity error
  linearly, from unmodelled acceleration and turning quadratically. A predicted point five seconds out
  and one sixty seconds out are not comparable claims, and drawing them the same way invites an
  operator to act on the second as if it were the first.
* **A stationary object gets a stationary prediction.** Extrapolating noise on a parked truck produces
  confident motion in a direction chosen by the last GPS jitter. Below a speed floor the answer is "it
  is where it is", with an interval covering the jitter.
* **Turning is decay, not a hard stop.** A vehicle mid-turn is not going to continue straight forever,
  so the predicted heading relaxes toward the recent turn rate while the speed decays toward zero over
  a horizon much longer than any single manoeuvre.

Next-zone prediction then asks which zone the cone enters, and reports a probability from the fraction
of sampled paths that land there rather than a single ray. One ray through a polygon boundary is a coin
flip presented as a fact.
"""

from __future__ import annotations

import itertools
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sio_core import get_logger
from sio_schemas import Geo

log = get_logger("sio.prediction.trajectory")

EARTH_RADIUS_M = 6_371_008.8

# Below this speed an object is parked, and its heading is whatever the noise says. Chosen a little
# above typical GPS jitter (about 0.3 m/s of apparent motion when stationary).
SPEED_FLOOR_MPS = 0.6
# Speed decays toward zero with this time constant. Long relative to a manoeuvre, short enough that a
# sixty-second prediction does not assume a truck keeps its current speed across the whole yard.
SPEED_DECAY_S = 45.0
# The turn rate decays faster than the speed: a vehicle mid-corner straightens out long before it stops.
TURN_DECAY_S = 8.0
# Beyond this positional uncertainty the prediction carries no usable information, and the honest move is
# to stop rather than to keep emitting points. A cone wider than a yard does not describe a yard.
MAX_USEFUL_SIGMA_M = 80.0
# The fastest turn any yard vehicle plausibly makes. Anything beyond this is a bad heading, not a
# manoeuvre, and it is discarded rather than averaged in.
MAX_TURN_RATE_DEG_S = 60.0


@dataclass
class Kinematics:
    """What the world model knows about how something is moving."""

    geo: Geo
    speed_mps: float
    heading_deg: float | None
    ts: datetime
    turn_rate_deg_s: float = 0.0
    position_sigma_m: float = 3.0
    """Current positional uncertainty. The cone starts at this width rather than at zero, because the
    present position is not known exactly either."""


@dataclass
class PredictedPoint:
    ts: datetime
    geo: Geo
    sigma_m: float
    """One-sigma positional uncertainty at this horizon."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts.isoformat(),
            "lat": round(self.geo.lat, 7),
            "lon": round(self.geo.lon, 7),
            "sigma_m": round(self.sigma_m, 2),
        }


@dataclass
class Trajectory:
    """A predicted path with its widening uncertainty."""

    entity_id: str
    points: list[PredictedPoint] = field(default_factory=list)
    stationary: bool = False
    notes: list[str] = field(default_factory=list)
    model_name: str = "damped_kinematic"

    @property
    def horizon_s(self) -> float:
        if not self.points:
            return 0.0
        return (self.points[-1].ts - self.points[0].ts).total_seconds()

    @property
    def final_sigma_m(self) -> float:
        return self.points[-1].sigma_m if self.points else 0.0

    def confidence(self) -> float:
        """How much to believe the endpoint.

        Falls with the final uncertainty, which is the only defensible basis: a 60 m cone is not a
        prediction anyone should plan around, whatever the model's opinion of its own fit.
        """
        if not self.points:
            return 0.0
        if self.stationary:
            return 0.8  # "it will still be there" is usually right, and cheap to check
        return round(max(0.05, min(0.9, 12.0 / (12.0 + self.final_sigma_m))), 3)


def predict_trajectory(
    entity_id: str,
    kinematics: Kinematics,
    *,
    horizon_s: float = 60.0,
    step_s: float = 5.0,
) -> Trajectory:
    """Extrapolate a path, with uncertainty growing along it."""
    steps = max(1, round(horizon_s / step_s))

    if kinematics.speed_mps < SPEED_FLOOR_MPS or kinematics.heading_deg is None:
        # Parked, or moving too slowly for its heading to be meaningful. Predicting motion here would
        # be extrapolating jitter, confidently, in a direction chosen by the last bad fix.
        points = [
            PredictedPoint(
                ts=kinematics.ts + timedelta(seconds=step_s * step),
                geo=kinematics.geo,
                # Even a parked object drifts in the record: uncertainty grows slowly with time since
                # the last fix, because "parked" is an inference and not an observation.
                sigma_m=kinematics.position_sigma_m + 0.15 * step_s * step,
            )
            for step in range(1, steps + 1)
        ]
        return Trajectory(
            entity_id=entity_id,
            points=points,
            stationary=True,
            notes=[
                f"speed {kinematics.speed_mps:.2f} m/s is below the {SPEED_FLOOR_MPS} m/s floor, "
                "so the prediction is that it stays put",
                "extrapolating a heading at this speed would be extrapolating GPS noise",
            ],
        )

    east, north = 0.0, 0.0
    heading = kinematics.heading_deg
    speed = kinematics.speed_mps
    turn_rate = kinematics.turn_rate_deg_s
    points: list[PredictedPoint] = []

    for step in range(1, steps + 1):
        elapsed = step_s * step
        # Decay both the speed and the turn rate. The turn decays faster: a vehicle straightens out of a
        # corner long before it comes to a stop.
        speed_now = speed * math.exp(-elapsed / SPEED_DECAY_S)
        turn_now = turn_rate * math.exp(-elapsed / TURN_DECAY_S)
        heading = (heading + turn_now * step_s) % 360.0
        radians = math.radians(heading)
        east += math.sin(radians) * speed_now * step_s
        north += math.cos(radians) * speed_now * step_s

        # Cone growth, scaled to the distance actually travelled rather than to elapsed time squared.
        #
        # The first version added a term for unmodelled acceleration: 0.5 * 0.35 m/s^2 * t^2. That is
        # dimensionally correct and physically nonsense for a yard vehicle — live it produced a 630 m
        # cone after 60 seconds for an object moving at 2.2 m/s, which is wider than the entire site and
        # therefore says nothing at all. Sustained acceleration for a full minute is not a thing a truck
        # in a dock apron does.
        #
        # Distance travelled is the natural scale for both error components:
        #   along-track — it might stop, or press on faster, so about half the distance covered;
        #   cross-track — a heading error of theta puts it travelled*sin(theta) to the side, and heading
        #                 confidence decays with time rather than with distance.
        travelled = math.hypot(east, north)
        along_sigma = 0.5 * travelled
        # Heading confidence depends on whether the object is actually turning. A truck running straight
        # down a lane is highly predictable; one mid-manoeuvre is not, and giving both the same cone
        # would be pessimistic about the first and optimistic about the second.
        heading_drift_per_s = 0.3 + abs(kinematics.turn_rate_deg_s) * 0.1
        heading_sigma_deg = min(60.0, 8.0 + heading_drift_per_s * elapsed)
        cross_sigma = travelled * math.sin(math.radians(heading_sigma_deg))
        sigma = math.hypot(kinematics.position_sigma_m, math.hypot(along_sigma, cross_sigma))
        points.append(
            PredictedPoint(
                ts=kinematics.ts + timedelta(seconds=elapsed),
                geo=_offset(kinematics.geo, east, north),
                sigma_m=sigma,
            )
        )
        if sigma > MAX_USEFUL_SIGMA_M:
            # Truncate. Continuing would produce points whose stated uncertainty already exceeds
            # anything an operator could act on, and a long list of them reads as a confident path.
            truncated = True
            break
    else:
        truncated = False

    if truncated:
        notes_tail = [
            f"truncated at {points[-1].ts.isoformat()}: uncertainty passed "
            f"{MAX_USEFUL_SIGMA_M:.0f} m, beyond which the prediction carries no usable information"
        ]
    else:
        notes_tail = []

    return Trajectory(
        entity_id=entity_id,
        points=points,
        notes=[
            f"heading {kinematics.heading_deg:.0f}° at {kinematics.speed_mps:.1f} m/s, "
            f"turning {kinematics.turn_rate_deg_s:+.1f}°/s",
            f"speed decays with a {SPEED_DECAY_S:.0f}s constant and the turn with {TURN_DECAY_S:.0f}s, "
            "because neither continues unchanged",
            f"uncertainty grows from {kinematics.position_sigma_m:.1f} m to "
            f"{points[-1].sigma_m:.1f} m over "
            f"{(points[-1].ts - kinematics.ts).total_seconds():.0f}s, scaled to the distance travelled "
            "rather than to elapsed time squared",
            *notes_tail,
        ],
    )


@dataclass
class ZonePrediction:
    zone_id: str
    probability: float
    eta_s: float | None
    """Seconds until entry, from the earliest sampled path that entered."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "zone_id": self.zone_id,
            "probability": round(self.probability, 3),
            "eta_s": round(self.eta_s, 1) if self.eta_s is not None else None,
        }


def predict_next_zones(
    trajectory: Trajectory,
    zone_contains: Any,
    *,
    samples: int = 60,
    seed: int = 1337,
    current_zones: tuple[str, ...] = (),
) -> list[ZonePrediction]:
    """Which zones the cone is likely to enter, and roughly when.

    Sampled rather than traced: a single ray passing near a polygon edge is a coin flip reported as a
    fact, and the interesting cases — a truck approaching a gate at an angle — are exactly the ones
    near an edge. Scattering paths across the uncertainty cone turns that into a probability, which is
    what the answer actually is.

    ``zone_contains`` is a callable ``Geo -> list[zone_id]``, so this stays independent of how zones are
    stored. The spatial service owns point-in-polygon; this must not become a second implementation.
    """
    if not trajectory.points:
        return []

    rng = random.Random(seed)
    first_entry: dict[str, float] = {}
    hits: dict[str, int] = {}

    for _ in range(samples):
        # One perturbation per path, held for its whole length, rather than fresh noise at every step.
        # Independent per-step noise would average out along the path and understate the spread — the
        # error in a trajectory is dominated by being wrong about the *velocity*, which persists.
        bearing_jitter = rng.gauss(0.0, 1.0)
        radial_jitter = rng.gauss(0.0, 1.0)
        seen: set[str] = set()
        for point in trajectory.points:
            # Scale the fixed perturbation by this point's sigma, so the sample tracks the cone.
            offset_east = radial_jitter * point.sigma_m * 0.7
            offset_north = bearing_jitter * point.sigma_m * 0.7
            probe = _offset(point.geo, offset_east, offset_north)
            for zone_id in zone_contains(probe):
                if zone_id in current_zones or zone_id in seen:
                    continue
                seen.add(zone_id)
                hits[zone_id] = hits.get(zone_id, 0) + 1
                elapsed = (point.ts - trajectory.points[0].ts).total_seconds()
                first_entry[zone_id] = min(first_entry.get(zone_id, elapsed), elapsed)

    predictions = [
        ZonePrediction(zone_id=zone_id, probability=count / samples, eta_s=first_entry.get(zone_id))
        for zone_id, count in hits.items()
    ]
    # Most likely first; ties broken by the sooner arrival, which is the more actionable of two equals.
    predictions.sort(key=lambda item: (-item.probability, item.eta_s or 1e9))
    return predictions


def turn_rate_from_headings(headings: list[tuple[datetime, float]]) -> float:
    """Estimate a turn rate in degrees per second from recent headings.

    Differences are wrapped into (-180, 180] before averaging. Without that, a vehicle crossing north
    produces a 359-degree "turn", and a mean over such values yields a turn rate that would have the
    prediction spiralling.
    """
    if len(headings) < 2:
        return 0.0
    rates: list[float] = []
    for (earlier_ts, earlier), (later_ts, later) in itertools.pairwise(headings):
        dt = (later_ts - earlier_ts).total_seconds()
        if dt <= 0:
            continue
        delta = (later - earlier + 180.0) % 360.0 - 180.0
        rate = delta / dt
        if abs(rate) > MAX_TURN_RATE_DEG_S:
            # Physically impossible, so it is a bad heading rather than a turn. Discarding beats
            # averaging: a spurious 180-degree flip yields TWO large differences of the same sign
            # (wrapping maps +180 and -180 to the same value), so they do not cancel and even a median
            # over four samples is dragged to -90 deg/s. Measured, not theorised — a test did exactly
            # this.
            continue
        rates.append(rate)
    if not rates:
        return 0.0
    rates.sort()
    # Median: one bad heading between two good ones produces two large equal-and-opposite rates, which
    # a mean would keep and a median discards.
    middle = len(rates) // 2
    return rates[middle] if len(rates) % 2 else (rates[middle - 1] + rates[middle]) / 2


def _offset(origin: Geo, east_m: float, north_m: float) -> Geo:
    delta_lat = north_m / EARTH_RADIUS_M
    delta_lon = east_m / (EARTH_RADIUS_M * math.cos(math.radians(origin.lat)))
    return Geo(lat=origin.lat + math.degrees(delta_lat), lon=origin.lon + math.degrees(delta_lon))
