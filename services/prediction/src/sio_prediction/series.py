"""Building regular time series out of irregular reality.

The least glamorous file in the service and the one most likely to make every forecast wrong.

Sensors report when they feel like it. A temperature sensor sends every five seconds until its link
drops for two minutes; entities appear and vanish; events arrive in bursts. Every forecasting method
worth using assumes **regularly spaced observations**, so something has to bridge the two — and the
bridging decisions matter more than the choice of model:

* **A missing count and a missing measurement are not the same thing.** No vehicles entered during a
  bucket is a real zero. No temperature reading during a bucket is *unknown*, and filling it with zero
  invents a freezing warehouse. So gap policy is per-series, declared by the caller, not a global
  default that is quietly wrong for half the metrics.
* **Trailing partial buckets must be dropped.** The bucket containing "now" is incomplete by
  definition, so a count in it is always low. Feeding it to a forecaster teaches the model that
  activity is collapsing, every single time it runs, and the resulting downward slope is entirely an
  artefact of asking the question.
* **A short series is not a series.** Refusing to forecast is a valid answer, and better than an
  interval computed from four points, which will be both narrow and wrong.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sio_core import get_logger

log = get_logger("sio.prediction.series")


class GapPolicy(StrEnum):
    """What an empty bucket means for this particular quantity."""

    ZERO = "zero"
    """Nothing happened, and that is data. Counts, throughput, event rates."""
    HOLD = "hold"
    """The quantity persists between reports. Temperature, occupancy, battery level."""
    DROP = "drop"
    """Unknown, and not worth guessing. Leaves a hole the forecaster is told about."""


@dataclass(frozen=True)
class Series:
    """A regularly spaced series, ready to forecast."""

    name: str
    bucket_s: float
    start: datetime
    values: tuple[float, ...]
    gaps: int = 0
    """Buckets that had no observation and were filled by the gap policy."""
    unit: str | None = None
    source: str = ""

    def __len__(self) -> int:
        return len(self.values)

    @property
    def timestamps(self) -> list[datetime]:
        return [self.start + timedelta(seconds=self.bucket_s * i) for i in range(len(self.values))]

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.bucket_s * max(0, len(self.values) - 1))

    @property
    def coverage(self) -> float:
        """Fraction of buckets that held a real observation.

        Reported alongside every forecast, because a forecast from a series that was 70 per cent
        invented deserves to be read differently from one that was fully observed — and nothing else in
        the output would ever reveal the difference.
        """
        return 1.0 - (self.gaps / len(self.values)) if self.values else 0.0

    @property
    def is_flat(self) -> bool:
        """Constant to within floating-point noise.

        Worth knowing before fitting: a model on a flat series produces a zero-width interval, which
        then claims certainty about the future of a sensor that may simply be stuck.
        """
        return len({round(value, 9) for value in self.values}) <= 1

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "points": len(self.values),
            "bucket_s": self.bucket_s,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "coverage": round(self.coverage, 3),
            "gaps": self.gaps,
            "mean": round(statistics.fmean(self.values), 4) if self.values else None,
            "last": self.values[-1] if self.values else None,
            "flat": self.is_flat,
        }


def bucketise(
    samples: Sequence[tuple[datetime, float]],
    *,
    name: str,
    bucket_s: float,
    now: datetime,
    policy: GapPolicy = GapPolicy.HOLD,
    aggregate: str = "mean",
    lookback_s: float | None = None,
    unit: str | None = None,
) -> Series | None:
    """Resample irregular ``(ts, value)`` samples onto a regular grid.

    Returns None when there is nothing to build a series from. ``now`` is passed in rather than read
    from the clock so this is deterministic under test — and so a replay can resample history exactly
    as the live path would have.
    """
    if not samples:
        return None

    ordered = sorted(samples, key=lambda item: item[0])
    horizon_start = now - timedelta(seconds=lookback_s) if lookback_s else ordered[0][0]
    ordered = [(ts, value) for ts, value in ordered if ts >= horizon_start]
    if not ordered:
        return None

    # Align the grid to bucket boundaries so repeated calls produce the same buckets. Without this the
    # grid slides with the wall clock and consecutive forecasts are not comparable.
    epoch = ordered[0][0].timestamp()
    aligned_start_epoch = math.floor(epoch / bucket_s) * bucket_s
    start = datetime.fromtimestamp(aligned_start_epoch, tz=ordered[0][0].tzinfo)

    # Drop the bucket containing `now`: it is incomplete, so its aggregate is biased low. For a count
    # that bias is a fake downward trend on every single run.
    last_complete_epoch = math.floor(now.timestamp() / bucket_s) * bucket_s
    bucket_count = int((last_complete_epoch - aligned_start_epoch) / bucket_s)
    if bucket_count <= 0:
        return None

    buckets: list[list[float]] = [[] for _ in range(bucket_count)]
    for ts, value in ordered:
        index = int((ts.timestamp() - aligned_start_epoch) / bucket_s)
        if 0 <= index < bucket_count:
            buckets[index].append(value)

    reducer = {
        "mean": statistics.fmean,
        "sum": sum,
        "max": max,
        "min": min,
        "count": len,
        "median": statistics.median,
        "last": lambda values: values[-1],
    }[aggregate]

    values: list[float] = []
    gaps = 0
    for bucket in buckets:
        if bucket:
            values.append(float(reducer(bucket)))
            continue
        gaps += 1
        if policy is GapPolicy.ZERO:
            values.append(0.0)
        elif policy is GapPolicy.HOLD:
            # Carry the last known value. Before any observation there is nothing to carry, so the
            # bucket is dropped instead of back-filled from the future — which would leak information
            # the forecaster could not have had.
            if values:
                values.append(values[-1])
            else:
                gaps -= 1  # nothing filled; the bucket simply does not exist yet
        else:
            if values:
                values.append(values[-1])
            else:
                gaps -= 1

    if not values:
        return None
    return Series(
        name=name,
        bucket_s=bucket_s,
        start=start,
        values=tuple(values),
        gaps=gaps,
        unit=unit,
    )


def counts_per_bucket(
    timestamps: Sequence[datetime],
    *,
    name: str,
    bucket_s: float,
    now: datetime,
    lookback_s: float | None = None,
) -> Series | None:
    """Count events per bucket — throughput, arrival rate, event rate.

    A separate entrypoint because the gap policy is not a choice here: an empty bucket means zero
    events, and any other filling would be a fabrication.
    """
    return bucketise(
        [(ts, 1.0) for ts in timestamps],
        name=name,
        bucket_s=bucket_s,
        now=now,
        policy=GapPolicy.ZERO,
        aggregate="sum",
        lookback_s=lookback_s,
        unit="per_bucket",
    )
