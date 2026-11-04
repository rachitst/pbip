"""Daily balance forecast with prediction intervals (TASK-042).

* 28+ days of history: statsforecast AutoARIMA with a weekly season. The rolling-origin backtest
  (``backtest.py``; 59 synthetic series, 233 thirty-day forecasts each, seed 42) chose it:
  sMAPE 10.3% vs 12.5% for AutoETS and 15.1% for seasonal naive, and the best-calibrated 80%
  interval (86.6% coverage vs AutoETS's over-wide 93.3%). It costs about 0.9 s per fit on a laptop
  (AutoETS about 0.2 s).
* Under 28 days: naive (last balance), with statsforecast's residual-based intervals. Weekly
  seasonal naive used to serve 14-27 days, but it repeats last week's balance LEVELS, so a one-off
  debit was projected to come back every week (#384). The short-history backtest
  (``backtest.py --short-history``; the same 59 series, 14/21/27-day windows, 235 forecasts each)
  agrees: naive sMAPE 12.4-12.5% vs 15.1-15.2%, 80% coverage 80.1-85.0% vs 74.1-76.1%. Naive's 95%
  interval is slightly narrow on 14-21 days (85.7-86.9% coverage; seasonal naive 87.1-89.0%).

statsforecast is imported lazily: it JIT-compiles with numba (about 2 s per process once cached),
which must not slow API start-up or unrelated tests.
"""

from collections.abc import Callable, Sequence
from datetime import date, timedelta

import numpy as np

from pbip_ml.forecasting.contract import Forecast, ForecastKind, ForecastPoint

MODEL_VERSION = "forecast-arima-2"
SEASON = 7
MIN_MODEL_DAYS = 28


def method_for(history_days: int) -> str:
    if history_days >= MIN_MODEL_DAYS:
        return "auto_arima"
    return "naive"


def _model(method: str) -> object:
    from statsforecast.models import AutoARIMA, AutoETS, Naive, SeasonalNaive  # lazy, see above

    factories: dict[str, Callable[[], object]] = {
        "auto_ets": lambda: AutoETS(season_length=SEASON),
        "auto_arima": lambda: AutoARIMA(season_length=SEASON),
        "seasonal_naive": lambda: SeasonalNaive(season_length=SEASON),
        "naive": lambda: Naive(),
    }
    return factories[method]()


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
