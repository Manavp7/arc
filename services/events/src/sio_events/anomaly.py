"""Anomaly detection for patterns no rule anticipated (PRD M9, UC6).

Rules cover what someone thought of. UC6 is the opposite requirement: notice that something is *odd*
without having been told what odd looks like, and — crucially — say which measurements were odd, because
"anomaly score 0.87" is not something an operator can act on.

**Why a statistical detector rather than PyOD's IsolationForest.**

The PRD names PyOD. PyOD's IForest wraps scikit-learn, and the honest trade here is unattractive: on the
feature vectors this service actually produces — a handful of counts and rates per minute, a few hundred
samples an hour — an IsolationForest is not measurably better than a robust z-score, and it is
considerably worse at the part that matters. A forest gives a score; attributing that score to
individual features needs SHAP or a permutation study, which is a second dependency and a per-alert
cost. A robust per-feature z-score is *inherently* attributable: the deviation per feature is the
output, not a post-hoc reconstruction.

So the default is `RobustZScoreDetector`, and `PyODDetector` is available behind the same interface for
when the feature space grows enough to justify it (correlated features and interaction effects are where
a forest genuinely wins). Both are wired through `build_detector`, so switching is configuration.

Robust statistics specifically — median and MAD rather than mean and standard deviation — because the
mean and standard deviation of a window that *contains* an outlier are both dragged toward it, which is
how a large anomaly hides itself. That failure is called masking, and it is exactly the case this
detector exists to catch.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sio_core import get_logger

log = get_logger("sio.events.anomaly")

# 0.6745 is the ratio of the MAD to the standard deviation for a normal distribution, so scaling by its
# reciprocal makes a robust z-score comparable to a familiar one: 3 still means "three sigma".
MAD_TO_SIGMA = 1.4826


@dataclass
class FeatureVector:
    """One observation of the world, summarised."""

    ts: datetime
    values: dict[str, float]
    subject: str = "site"

    def get(self, name: str) -> float | None:
        return self.values.get(name)


@dataclass
class AnomalyVerdict:
    """A judgement with its reasoning attached."""

    is_anomaly: bool
    score: float
    subject: str
    ts: datetime
    deviations: list[tuple[str, float, float, float]] = field(default_factory=list)
    """(feature, observed, baseline, z) sorted by |z| descending."""
    reasons: list[str] = field(default_factory=list)
    samples: int = 0
    detector: str = ""

    @property
    def top_features(self) -> list[str]:
        return [name for name, *_ in self.deviations[:3]]


class Detector(Protocol):
    """The seam. Any detector that can score a vector and attribute the score is usable."""

    name: str

    def observe(self, vector: FeatureVector) -> AnomalyVerdict: ...

    def describe(self) -> dict[str, Any]: ...


class RobustZScoreDetector:
    """Per-feature robust z-scores over a rolling window, combined into one score.

    Median and MAD rather than mean and standard deviation: both of the latter are dragged toward an
    outlier that is *inside* the window, which lets a large anomaly mask itself. That is the case this
    detector exists to catch, so using statistics with that weakness would be self-defeating.
    """

    name = "robust_zscore"

    def __init__(
        self,
        *,
        window: int = 240,
        warmup: int = 30,
        z_threshold: float = 4.0,
        min_features: int = 1,
    ) -> None:
        self.window = window
        self.warmup = warmup
        self.z_threshold = z_threshold
        self.min_features = min_features
        self._history: dict[str, deque[float]] = {}
        self._subjects: dict[str, int] = {}
        self.stats: dict[str, int] = {"observed": 0, "flagged": 0, "warming": 0}

    def observe(self, vector: FeatureVector) -> AnomalyVerdict:
        self.stats["observed"] += 1
        self._subjects[vector.subject] = self._subjects.get(vector.subject, 0) + 1
        samples = self._subjects[vector.subject]

        deviations: list[tuple[str, float, float, float]] = []
        for name, value in vector.values.items():
            key = f"{vector.subject}:{name}"
            history = self._history.setdefault(key, deque(maxlen=self.window))
            if len(history) >= self.warmup:
                baseline = statistics.median(history)
                spread = _median_absolute_deviation(history, baseline)
                z = _robust_z(value, baseline, spread)
                if abs(z) >= self.z_threshold:
                    deviations.append((name, value, baseline, z))
            # Appended AFTER scoring, so a value is never compared against a baseline it helped set.
            history.append(value)

        if samples < self.warmup:
            self.stats["warming"] += 1
            return AnomalyVerdict(
                is_anomaly=False,
                score=0.0,
                subject=vector.subject,
                ts=vector.ts,
                samples=samples,
                detector=self.name,
                reasons=[f"still learning: {samples}/{self.warmup} samples for {vector.subject}"],
            )

        deviations.sort(key=lambda item: -abs(item[3]))
        is_anomaly = len(deviations) >= self.min_features
        # Squash the largest deviation into (0, 1). Reporting a raw z would make one wild feature
        # produce a "score" of 40, and a score with no ceiling cannot be compared or ranked.
        score = 1.0 - math.exp(-abs(deviations[0][3]) / 6.0) if deviations else 0.0
        reasons = [
            f"{name} was {observed:.3g}, baseline {baseline:.3g} ({z:+.1f} robust sigma)"
            for name, observed, baseline, z in deviations[:4]
        ]
        if is_anomaly:
            self.stats["flagged"] += 1
        return AnomalyVerdict(
            is_anomaly=is_anomaly,
            score=round(score, 3),
            subject=vector.subject,
            ts=vector.ts,
            deviations=deviations,
            reasons=reasons,
            samples=samples,
            detector=self.name,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "detector": self.name,
            "window": self.window,
            "warmup": self.warmup,
            "z_threshold": self.z_threshold,
            "features_tracked": len(self._history),
            "subjects": dict(self._subjects),
            "stats": dict(self.stats),
        }


class PyODDetector:
    """IsolationForest via PyOD, behind the same interface.

    Available for when the feature space grows enough that interaction effects matter — a forest can
    catch "throughput normal AND occupancy normal BUT the combination never happens", which no
    per-feature test can. It is not the default because attributing its score back to features needs
    SHAP or a permutation study, and an alert an operator cannot interrogate gets ignored.

    Attribution here is done by leave-one-feature-out perturbation: re-score with each feature replaced
    by its median and see how far the score moves. Honest, and O(features) model calls per alert, which
    is the cost the default avoids.
    """

    name = "pyod_iforest"

    def __init__(
        self, *, window: int = 400, warmup: int = 200, contamination: float = 0.02
    ) -> None:
        self.window = window
        self.warmup = warmup
        self.contamination = contamination
        self._rows: deque[list[float]] = deque(maxlen=window)
        self._feature_names: list[str] = []
        self._model: Any = None
        self._fitted_at = 0
        self.stats: dict[str, int] = {"observed": 0, "flagged": 0, "warming": 0, "fits": 0}
        self._available = self._probe()

    @staticmethod
    def _probe() -> bool:
        try:
            import pyod  # noqa: F401
        except ImportError:
            log.warning(
                "anomaly.pyod_missing",
                effect="falling back to the robust z-score detector",
                hint="uv add --optional anomaly pyod scikit-learn",
            )
            return False
        return True

    def observe(self, vector: FeatureVector) -> AnomalyVerdict:
        if not self._available:
            raise RuntimeError("PyOD is not installed; build_detector should have fallen back")
        import numpy as np

        self.stats["observed"] += 1
        if not self._feature_names:
            self._feature_names = sorted(vector.values)
        row = [float(vector.values.get(name, 0.0)) for name in self._feature_names]
        self._rows.append(row)

        if len(self._rows) < self.warmup:
            self.stats["warming"] += 1
            return AnomalyVerdict(
                is_anomaly=False,
                score=0.0,
                subject=vector.subject,
                ts=vector.ts,
                samples=len(self._rows),
                detector=self.name,
                reasons=[f"still learning: {len(self._rows)}/{self.warmup} samples"],
            )

        if self._model is None or len(self._rows) - self._fitted_at >= self.warmup // 4:
            from pyod.models.iforest import IForest

            self._model = IForest(contamination=self.contamination, random_state=1337)
            self._model.fit(np.asarray(self._rows, dtype=np.float64))
            self._fitted_at = len(self._rows)
            self.stats["fits"] += 1

        array = np.asarray([row], dtype=np.float64)
        score = float(self._model.decision_function(array)[0])
        is_anomaly = bool(self._model.predict(array)[0])

        # Leave-one-out attribution: replace each feature with its median and see how much the score
        # moves. The features whose removal most reduces the score are the ones driving it.
        deviations: list[tuple[str, float, float, float]] = []
        history = np.asarray(self._rows, dtype=np.float64)
        for index, name in enumerate(self._feature_names):
            median = float(np.median(history[:, index]))
            perturbed = array.copy()
            perturbed[0, index] = median
            delta = score - float(self._model.decision_function(perturbed)[0])
            if abs(delta) > 1e-9:
                deviations.append((name, row[index], median, delta))
        deviations.sort(key=lambda item: -abs(item[3]))
        if is_anomaly:
            self.stats["flagged"] += 1
        return AnomalyVerdict(
            is_anomaly=is_anomaly,
            score=round(float(score), 3),
            subject=vector.subject,
            ts=vector.ts,
            deviations=deviations,
            reasons=[
                f"{name} was {observed:.3g} against a median of {median:.3g} "
                f"(contributes {delta:+.3f} to the score)"
                for name, observed, median, delta in deviations[:4]
            ],
            samples=len(self._rows),
            detector=self.name,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "detector": self.name,
            "available": self._available,
            "window": self.window,
            "warmup": self.warmup,
            "contamination": self.contamination,
            "features": list(self._feature_names),
            "stats": dict(self.stats),
        }


def build_detector(
    kind: str = "auto", *, warmup: int = 30, contamination: float = 0.02
) -> Detector:
    """Pick a detector. ``auto`` prefers the attributable one and never fails to return something."""
    if kind in ("pyod", "iforest"):
        detector = PyODDetector(warmup=max(warmup, 50), contamination=contamination)
        if detector._available:
            return detector
        log.warning("anomaly.falling_back", requested=kind, using="robust_zscore")
    return RobustZScoreDetector(warmup=warmup)


def _median_absolute_deviation(values: deque[float], median: float) -> float:
    return statistics.median([abs(value - median) for value in values]) if values else 0.0


def _robust_z(value: float, median: float, mad: float) -> float:
    """Robust z-score, with a floor on the spread.

    A window of identical values has a MAD of zero, and dividing by it would make any change whatsoever
    infinitely anomalous — a sensor reporting exactly 20.0 for an hour and then 20.1 is not an incident.
    The floor is a hundredth of the magnitude, so it scales with the quantity instead of assuming units.
    """
    spread = mad * MAD_TO_SIGMA
    floor = max(1e-6, abs(median) * 0.01)
    return (value - median) / max(spread, floor)
