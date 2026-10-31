"""Daily balance forecast with prediction intervals (TASK-042).

* 28+ days of history: statsforecast AutoARIMA with a weekly season. The rolling-origin backtest
  (``backtest.py``; 59 synthetic series, 233 thirty-day forecasts each, seed 42) chose it:
  sMAPE 10.3% vs 12.5% for AutoETS and 15.1% for seasonal naive, and the best-calibrated 80%
  interval (86.6% coverage vs AutoETS's over-wide 93.3%). It costs about 0.9 s per fit on a laptop
  (AutoETS about 0.2 s).
* 14-27 days: seasonal naive (weekly): too little history to fit a model honestly.
* Under 14 days: naive (last balance). Intervals still come from statsforecast's residual model.

statsforecast is imported lazily: it JIT-compiles with numba (about 2 s per process once cached),
which must not slow API start-up or unrelated tests.
"""

from collections.abc import Sequence
from datetime import date, timedelta

import numpy as np

from pbip_ml.forecasting.contract import Forecast, ForecastKind, ForecastPoint

MODEL_VERSION = "forecast-arima-1"
SEASON = 7
MIN_MODEL_DAYS = 28
MIN_SEASONAL_DAYS = 14


def method_for(history_days: int) -> str:
    if history_days >= MIN_MODEL_DAYS:
        return "auto_arima"
    if history_days >= MIN_SEASONAL_DAYS:
        return "seasonal_naive"
    return "naive"


def _model(method: str) -> object:
    from statsforecast.models import AutoARIMA, AutoETS, Naive, SeasonalNaive  # lazy, see above

    return {
        "auto_ets": lambda: AutoETS(season_length=SEASON),
        "auto_arima": lambda: AutoARIMA(season_length=SEASON),
        "seasonal_naive": lambda: SeasonalNaive(season_length=SEASON),
        "naive": lambda: Naive(),
    }[method]()


def predict(
    history: Sequence[float], horizon: int, method: str, levels: Sequence[int]
) -> dict[str, np.ndarray]:
    """Raw statsforecast output: ``mean`` plus ``lo-L`` / ``hi-L`` for every level."""
    if len(history) < 2:
        raise ValueError("need at least two days of history")
    y = np.asarray(history, dtype=float)
    fitted = _model(method).fit(y)  # type: ignore[attr-defined]  # statsforecast models
    out: dict[str, np.ndarray] = fitted.predict(h=horizon, level=list(levels))
    return out


def forecast_daily_balance(
    history_minor: Sequence[int], horizon: int, first_day: date, levels: Sequence[int] = (80, 95)
) -> list[Forecast]:
    """One ``Forecast`` per interval level, starting at ``first_day`` (the day after history)."""
    method = method_for(len(history_minor))
    raw = predict(history_minor, horizon, method, levels)
    forecasts = []
    for level in levels:
        lo, hi, mean = raw[f"lo-{level}"], raw[f"hi-{level}"], raw["mean"]
        points = [
            ForecastPoint(
                period_start=first_day + timedelta(days=i),
                lower=float(min(lo[i], mean[i])),
                median=float(mean[i]),
                upper=float(max(hi[i], mean[i])),
            )
            for i in range(horizon)
        ]
        forecasts.append(
            Forecast(
                kind=ForecastKind.DAILY_BALANCE,
                confidence_level=level / 100,
                points=points,
                model_version=MODEL_VERSION,
                method=method,
            )
        )
    return forecasts
