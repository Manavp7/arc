"""SIO forecasting (PRD M10)."""

from .forecasters import (
    Backtest,
    DriftForecaster,
    ForecastResult,
    StatsForecastForecaster,
    backtest,
    forecast_series,
    select_forecaster,
)
from .series import GapPolicy, Series, bucketise, counts_per_bucket
from .service import PredictionService
from .targets import (
    SPECS,
    TargetForecast,
    TargetSpec,
    build,
    congestion_from_occupancy,
    time_to_threshold,
)
from .trajectory import (
    Kinematics,
    PredictedPoint,
    Trajectory,
    ZonePrediction,
    predict_next_zones,
    predict_trajectory,
    turn_rate_from_headings,
)

__all__ = [
    "SPECS",
    "Backtest",
    "DriftForecaster",
    "ForecastResult",
    "GapPolicy",
    "Kinematics",
    "PredictedPoint",
    "PredictionService",
    "Series",
    "StatsForecastForecaster",
    "TargetForecast",
    "TargetSpec",
    "Trajectory",
    "ZonePrediction",
    "backtest",
    "bucketise",
    "build",
    "congestion_from_occupancy",
    "counts_per_bucket",
    "forecast_series",
    "predict_next_zones",
    "predict_trajectory",
    "select_forecaster",
    "time_to_threshold",
    "turn_rate_from_headings",
]
