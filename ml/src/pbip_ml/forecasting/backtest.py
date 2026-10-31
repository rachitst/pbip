"""Rolling-origin backtest of the balance forecast (TASK-042).

For every series, forecasts are made from several origins (each using only the data before it)
and compared with what actually happened next: sMAPE for the point forecast and the share of actual
values inside the 80% / 95% intervals (coverage). Series come from the deterministic synthetic
dataset (data-gen v1), so the numbers are reproducible:

    uv run python -m pbip_ml.forecasting.backtest --customers 60 --months 12 [--json out.json]
"""

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

import numpy as np
from numpy.typing import ArrayLike

from pbip_ml.forecasting.model import predict


def smape(actual: ArrayLike, predicted: ArrayLike) -> float:
    """Symmetric MAPE in percent; a pair of zeros counts as a perfect match."""
    a, p = np.asarray(actual, float), np.asarray(predicted, float)
    denom = np.abs(a) + np.abs(p)
    terms = np.where(denom == 0, 0.0, 2 * np.abs(p - a) / np.where(denom == 0, 1, denom))
    return float(100 * terms.mean())


def coverage(actual: ArrayLike, lower: ArrayLike, upper: ArrayLike) -> float:
    a = np.asarray(actual, float)
    return float(100 * np.mean((a >= np.asarray(lower)) & (a <= np.asarray(upper))))


def rolling_origin(
    series: Sequence[float], method: str, horizon: int, origins: int, step: int, min_train: int
) -> list[dict[str, float]]:
    results = []
    last_origin = len(series) - horizon
    first_origin = max(min_train, last_origin - step * (origins - 1))
    for origin in range(first_origin, last_origin + 1, step):
        raw = predict(series[:origin], horizon, method, (80, 95))
        actual = series[origin : origin + horizon]
        results.append(
            {
                "smape": smape(actual, raw["mean"]),
                "coverage_80": coverage(actual, raw["lo-80"], raw["hi-80"]),
                "coverage_95": coverage(actual, raw["lo-95"], raw["hi-95"]),
            }
        )
    return results


def synthetic_balance_series(customers: int, months: int, seed: int) -> list[list[float]]:
    """End-of-day balances (rupees) per customer from data-gen v1, opening at two months' income."""
    from pbip_datagen.generator import GeneratorConfig, customer_transactions, iter_customers

    config = GeneratorConfig(seed=seed, customers=customers, months=months)
    series = []
    for customer in iter_customers(config):
        txns = customer_transactions(config, customer)
        if not txns:
            continue
        day, end = txns[0].occurred_at.date(), txns[-1].occurred_at.date()
        balance = customer.monthly_income_minor * 2
        by_day: dict[object, int] = {}
        for t in txns:
            balance += t.amount_minor if t.direction == "CREDIT" else -t.amount_minor
            by_day[t.occurred_at.date()] = balance
        daily, last = [], customer.monthly_income_minor * 2
        while day <= end:
            last = by_day.get(day, last)
            daily.append(last / 100)
            day += timedelta(days=1)
        series.append(daily)
    return series


def run(customers: int, months: int, seed: int, horizon: int) -> dict[str, object]:
    series = [s for s in synthetic_balance_series(customers, months, seed) if len(s) >= 120]
    report: dict[str, object] = {
        "series": len(series),
        "horizon_days": horizon,
        "origins_per_series": 4,
        "seed": seed,
        "methods": {},
    }
    for method in ("auto_ets", "auto_arima", "seasonal_naive"):
        rows = [r for s in series for r in rolling_origin(s, method, horizon, 4, 30, 60)]
        report["methods"][method] = {  # type: ignore[index]
            "forecasts": len(rows),
            "smape_mean": round(float(np.mean([r["smape"] for r in rows])), 2),
            "smape_median": round(float(np.median([r["smape"] for r in rows])), 2),
            "coverage_80": round(float(np.mean([r["coverage_80"] for r in rows])), 1),
            "coverage_95": round(float(np.mean([r["coverage_95"] for r in rows])), 1),
        }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--customers", type=int, default=60)
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--horizon", type=int, default=30)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    report = run(args.customers, args.months, args.seed, args.horizon)
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
