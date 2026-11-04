"""TASK-042: balance forecast intervals and the backtest metrics."""

from datetime import date

import numpy as np
import pytest
from pbip_ml.forecasting.backtest import coverage, rolling_origin, short_window, smape
from pbip_ml.forecasting.model import forecast_daily_balance, method_for

pytestmark = pytest.mark.unit


def _series(days: int) -> list[int]:
    rng = np.random.default_rng(3)
    weekly = 5_000_00 * np.sin(np.arange(days) * 2 * np.pi / 7)
    return [int(v) for v in 2_00_000_00 + np.cumsum(rng.normal(0, 2_000_00, days)) + weekly]


def test_method_depends_on_history_length() -> None:
    assert (method_for(90), method_for(28), method_for(27), method_for(14), method_for(13)) == (
        "auto_arima",
        "auto_arima",
        "naive",
        "naive",
        "naive",
    )


@pytest.mark.parametrize("days", [14, 20, 27])
def test_a_one_off_debit_in_a_short_history_is_not_projected_to_come_back(days: int) -> None:
    """#384: weekly seasonal naive repeated last week's LEVELS, so a one-off step down six days
    ago swung back to the old balance every week (Alice: 30-day point INR 2,50,000 against a
    ledger balance of INR 2,00,072). The projection must stay at the new level."""
    stepped = [2_50_000_00] * (days - 6) + [2_00_072_00] * 6
    f80, f95 = forecast_daily_balance(stepped, 30, date(2026, 11, 3))
    assert {p.median for p in f80.points} == {2_00_072_00}
    assert all(p.lower <= 2_00_072_00 <= p.upper for p in f95.points)


@pytest.mark.parametrize("days", [10, 20, 90])
def test_forecast_has_ordered_nested_intervals(days: int) -> None:
    f80, f95 = forecast_daily_balance(_series(days), 30, date(2026, 11, 1))
    assert (f80.confidence_level, f95.confidence_level) == (0.8, 0.95)
    assert len(f80.points) == len(f95.points) == 30
    assert f80.points[0].period_start == date(2026, 11, 1)
    assert f80.method == f95.method == method_for(days)
    for a, b in zip(f80.points, f95.points, strict=True):
        assert b.lower <= a.lower <= a.median <= a.upper <= b.upper  # 95% contains 80%


def test_smape_and_coverage_exact_values() -> None:
    assert smape([100, 200], [110, 180]) == pytest.approx(100 * (20 / 210 + 40 / 380) / 2)
    assert smape([0, 0], [0, 0]) == 0.0
    assert coverage([1, 5, 9], [0, 6, 8], [2, 7, 10]) == pytest.approx(200 / 3)


def test_rolling_origin_uses_only_past_data_for_each_origin() -> None:
    series = [float(v) for v in _series(150)]
    rows = rolling_origin(series, "seasonal_naive", horizon=30, origins=4, step=30, min_train=60)
    assert len(rows) == 3  # origins 60, 90, 120 (the last whose 30 days are all known)
    assert all(0 <= r["coverage_80"] <= 100 and r["smape"] >= 0 for r in rows)


def test_short_window_sees_only_the_window_before_each_origin() -> None:
    # A level that jumps at day 100: a 14-day naive window ending before the jump predicts the old
    # level, so the window, not the whole prefix, is what was fitted.
    series = [1000.0] * 100 + [5000.0] * 50
    rows = short_window(series, "naive", horizon=30, window=14, origins=4, step=30)
    assert len(rows) == 4  # origins 30, 60, 90, 120
    assert [r["smape"] for r in rows[:2]] == [0.0, 0.0]
    assert rows[3]["smape"] == 0.0 and rows[2]["smape"] > 0
