# Balance forecast (TASK-042)

`GET /api/v1/analytics/forecast?account_id=…&horizon_days=30` projects an owned account's end-of-day
balance with 80% and 95% prediction intervals. Every response carries
`label: "Predicted, not exact"`.

## Model choice by history length
| History (days) | Method | Why |
|---|---|---|
| 28 or more | statsforecast **AutoARIMA**, weekly season | Best in the backtest below |
| 14-27 | Seasonal naive (weekly) | Too little history to fit a model honestly |
| Under 14 | Naive (last balance) | |

## Rolling-origin backtest (real run)
`uv run python -m pbip_ml.forecasting.backtest --customers 60 --months 12` with seed 42: 59 synthetic
balance series (data-gen v1, opening balance two months' income), 4 origins per series 30 days apart,
each using only the data before it, 30-day horizon, 233 forecasts per method.

| Method | sMAPE mean | sMAPE median | 80% coverage | 95% coverage |
|---|---|---|---|---|
| AutoARIMA | **10.31%** | **8.56%** | **86.6%** | 96.4% |
| AutoETS | 12.48% | 10.89% | 93.3% | 97.7% |
| Seasonal naive | 15.13% | 14.39% | 85.8% | 94.9% |

AutoARIMA has the lowest error and the best-calibrated 80% interval (AutoETS's is too wide).
Coverage above the nominal level means the intervals are slightly conservative.

## Limits
* Synthetic data only: real customer behaviour will differ; re-run the backtest on real history.
* AutoARIMA costs about 0.9 s per fit on a 2-CPU laptop (AutoETS about 0.2 s); statsforecast's
  numba compile adds about 2 s to the first forecast in each API process. Not measured in staging.
* The history is the ledger only: scheduled payments and salary dates are not used as regressors.
