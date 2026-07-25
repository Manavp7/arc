"""KPIs, computed from the append-only record (PRD M19, Phase 6).

Everything here is derived from `events`, `entities` and `relationships` — never from a separate counter that
a service increments. A counter drifts the moment a service restarts, and it cannot be recomputed for a past
window; a query over an append-only table can be re-run for any window and gives the same answer twice.

**The distributions matter more than the means, and that is the point of this module.** A mean dwell time of
18 minutes describes a yard where every truck takes 18 minutes and a yard where half take 4 and half take 32
identically — and those are different sites with different problems. So dwell is returned as a histogram with
percentiles, and the report says which shape it found.

Three things are deliberately *not* here:

* **No materialised views yet.** The PRD says "materialised rollups", and they are the right answer at a scale
  this deployment has not reached. On a yard's worth of data these queries run in single-digit milliseconds,
  and a materialised view is a second source of truth that can be stale — worth adding when a query gets slow,
  not before, and the refresh policy is the hard part.
* **No risk score invented from thin air.** The "risk index" the PRD names is computed from things the platform
  actually observes, and its formula is returned with the number so nobody has to guess what it means.
* **No smoothing.** A spiky throughput chart is a true chart of a spiky yard.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any

#: Percentiles reported for every distribution.
#:
#: p50 and p95 rather than mean and standard deviation: the underlying distributions are bounded below by zero
#: and have long right tails, so a standard deviation implies a symmetry that is not there and a mean sits
#: above most of the mass.
PERCENTILES = (50, 90, 95, 99)

#: Histogram edges for dwell time, in minutes.
#:
#: Chosen to match how a yard is actually discussed — under five minutes is a pass-through, over an hour is a
#: problem — rather than by dividing the range into equal parts, which would put every bucket boundary
#: somewhere nobody cares about.
#: These are UPPER bounds, not edges including zero.
#:
#: The leading 0 in the first version produced a "0 to 0: 0 visits (0%)" row in every single report — the
#: bucket "value < 0", which is empty by construction. A degenerate row in a user-visible table is the kind of
#: small wrongness that makes a reader distrust the numbers next to it.
DWELL_BUCKETS_MIN = (5, 15, 30, 60, 120, 240)


@dataclass
class Distribution:
    """A summarised set of measurements, with its shape described rather than only its centre."""

    name: str
    unit: str
    count: int
    mean: float = 0.0
    percentiles: dict[str, float] = field(default_factory=dict)
    histogram: list[dict[str, Any]] = field(default_factory=list)
    shape: str = ""

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "unit": self.unit,
            "count": self.count,
            "mean": round(self.mean, 2),
            "percentiles": {key: round(value, 2) for key, value in self.percentiles.items()},
            "histogram": self.histogram,
            "shape": self.shape,
        }


def summarise(name: str, unit: str, values: list[float], buckets: tuple[int, ...]) -> Distribution:
    """Build a distribution, and say what shape it is.

    The shape sentence exists because a number nobody interprets is a number nobody uses. "Bimodal" is
    actionable — it usually means two populations sharing one queue — where a mean and a standard deviation
    leave the reader to notice that themselves, which they will not.
    """
    if not values:
        return Distribution(name=name, unit=unit, count=0, shape="no measurements in this window")

    ordered = sorted(values)
    distribution = Distribution(
        name=name,
        unit=unit,
        count=len(ordered),
        mean=statistics.fmean(ordered),
        percentiles={f"p{p}": _percentile(ordered, p) for p in PERCENTILES},
    )

    counts = [0] * (len(buckets) + 1)
    for value in ordered:
        index = len(buckets)
        for position, edge in enumerate(buckets):
            if value < edge:
                index = position
                break
        counts[index] += 1
    distribution.histogram = [
        {
            "from": buckets[position - 1] if position else 0,
            "to": buckets[position] if position < len(buckets) else None,
            "count": count,
            "share": round(count / len(ordered), 3),
        }
        for position, count in enumerate(counts)
        if count or position < len(buckets)
    ]
    distribution.shape = _shape_of(ordered, distribution)
    return distribution


def _percentile(ordered: list[float], percentile: int) -> float:
    """Linear interpolation between order statistics.

    Not the nearest-rank method, which on small samples makes p95 and p99 identical — and a report where two
    percentiles always agree teaches the reader to ignore both.
    """
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _shape_of(ordered: list[float], distribution: Distribution) -> str:
    """Name the distribution's shape in a sentence an operator can act on."""
    if len(ordered) < 8:
        return f"only {len(ordered)} measurements; too few to describe a shape"

    median = distribution.percentiles["p50"]
    p95 = distribution.percentiles["p95"]
    mean = distribution.mean

    # Bimodality, detected crudely and honestly: a wide gap in the middle of the sorted values with mass on
    # both sides. A proper test (dip, Hartigan) needs more data than a yard produces in an hour, and a crude
    # detector that says "this looks bimodal, check it" beats a rigorous one that never fires.
    lower_half = [value for value in ordered if value <= median]
    upper_half = [value for value in ordered if value > median]
    if lower_half and upper_half:
        gap = min(upper_half) - max(lower_half)
        spread = ordered[-1] - ordered[0]
        if spread > 0 and gap / spread > 0.25:
            return (
                f"looks bimodal: a gap of {gap:.1f} separates two groups, which usually means two "
                f"populations sharing one queue rather than one population with variance"
            )

    if median > 0 and p95 / median > 4:
        return (
            f"long right tail: p95 ({p95:.1f}) is {p95 / median:.1f}x the median ({median:.1f}), so the "
            f"mean of {mean:.1f} describes almost nobody"
        )
    if median > 0 and abs(mean - median) / median < 0.15:
        return f"roughly symmetric around {median:.1f}, so the mean is a fair summary"
    return f"skewed: mean {mean:.1f} against median {median:.1f}"


# ------------------------------------------------------------------------------- risk index
#: Weights for the risk index.
#:
#: Every term is something the platform OBSERVES, which is the constraint that kept this from becoming
#: astrology. A risk score built from an invented "asset criticality tier" or a "threat level" would be a
#: number with no way to check it; these five can each be traced to rows in the database.
RISK_WEIGHTS: dict[str, float] = {
    "open_criticals": 25.0,
    "unacknowledged_ratio": 20.0,
    "blind_spot_ratio": 15.0,
    "restricted_occupancy": 20.0,
    "anomaly_rate": 20.0,
}


@dataclass
class RiskIndex:
    """A 0-100 index, with its terms shown.

    The terms travel with the number. A single risk score is the most misusable output in a platform like
    this — it will end up on a wall — and one that cannot be decomposed invites an argument nobody can settle.
    """

    score: float
    terms: dict[str, dict[str, Any]] = field(default_factory=dict)
    formula: str = ""
    drivers: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 1),
            "band": self.band,
            "terms": self.terms,
            "formula": self.formula,
            "drivers": self.drivers,
        }

    @property
    def band(self) -> str:
        if self.score >= 70:
            return "high"
        if self.score >= 40:
            return "elevated"
        if self.score >= 15:
            return "moderate"
        return "low"


def risk_index(
    *,
    open_criticals: int,
    open_alerts: int,
    unacknowledged: int,
    zones_total: int,
    zones_uncovered: int,
    restricted_occupied: int,
    restricted_total: int,
    anomalies_last_hour: int,
    events_last_hour: int,
) -> RiskIndex:
    """Compute the index from observed quantities only.

    Each term is normalised to 0-1 before weighting, so a site with three zones and one with thirty produce
    comparable numbers — an absolute count would make a large site permanently "high risk" for being large.
    """
    terms: dict[str, dict[str, Any]] = {}

    def term(name: str, value: float, why: str) -> float:
        clamped = max(0.0, min(1.0, value))
        contribution = clamped * RISK_WEIGHTS[name]
        terms[name] = {
            "normalised": round(clamped, 3),
            "weight": RISK_WEIGHTS[name],
            "contributes": round(contribution, 1),
            "why": why,
        }
        return contribution

    total = 0.0
    # Three open criticals saturates this term. Beyond that the difference between three and eight is not a
    # difference in how urgently somebody should look — both mean "look now".
    total += term(
        "open_criticals",
        open_criticals / 3.0,
        f"{open_criticals} critical alert(s) open; 3 or more saturates this term",
    )
    total += term(
        "unacknowledged_ratio",
        unacknowledged / open_alerts if open_alerts else 0.0,
        f"{unacknowledged} of {open_alerts} open alert(s) unacknowledged",
    )
    total += term(
        "blind_spot_ratio",
        zones_uncovered / zones_total if zones_total else 0.0,
        f"{zones_uncovered} of {zones_total} zone(s) have no camera covering them",
    )
    total += term(
        "restricted_occupancy",
        restricted_occupied / restricted_total if restricted_total else 0.0,
        f"{restricted_occupied} of {restricted_total} restricted zone(s) occupied",
    )
    total += term(
        "anomaly_rate",
        anomalies_last_hour / max(events_last_hour, 1) * 5,
        f"{anomalies_last_hour} anomaly event(s) out of {events_last_hour} in the last hour",
    )

    index = RiskIndex(
        score=total,
        terms=terms,
        formula=" + ".join(f"{weight:g}x{name}" for name, weight in RISK_WEIGHTS.items()),
    )
    # Drivers, ordered, so the number comes with the reason it is what it is. A score of 62 tells nobody what
    # to do; "62, mostly because 4 of 17 zones have no camera" does.
    index.drivers = [
        f"{name.replace('_', ' ')}: {detail['why']}"
        for name, detail in sorted(terms.items(), key=lambda pair: -pair[1]["contributes"])
        if detail["contributes"] > 0.5
    ]
    if not index.drivers:
        index.drivers = ["nothing is contributing materially to risk in this window"]
    return index


# --------------------------------------------------------------------------- utilisation
def utilisation(*, busy_seconds: float, window_seconds: float) -> float:
    """Fraction of a window a resource was in use, clamped to [0, 1].

    Clamped because overlapping intervals in the source data can sum past the window — two dwell records for
    one dock that overlap by a second would otherwise report 101 % utilisation, which reads as a bug in the
    dashboard rather than in the data.
    """
    if window_seconds <= 0:
        return 0.0
    return max(0.0, min(1.0, busy_seconds / window_seconds))


__all__ = [
    "DWELL_BUCKETS_MIN",
    "PERCENTILES",
    "RISK_WEIGHTS",
    "Distribution",
    "RiskIndex",
    "risk_index",
    "summarise",
    "utilisation",
]
