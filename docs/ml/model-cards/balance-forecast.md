# Model card: balance forecast

| | |
|---|---|
| Code | `ml/src/pbip_ml/forecasting/model.py` (serving), `ml/src/pbip_ml/forecasting/backtest.py` (evaluation), `apps/api/src/pbip_api/modules/analytics/projection.py` (obligations arithmetic) |
| `model_version` | `forecast-arima-1` (`MODEL_VERSION` in `model.py`), returned on every forecast |
| Served | `GET /api/v1/analytics/forecast?account_id=...&horizon_days=30` |
| Owner | `rachitst` (`.github/CODEOWNERS`, rule `*`) |
| Tasks | TASK-042, TASK-043, TASK-050 |

## Intended use

Project one owned account's end-of-day balance with 80% and 95% prediction intervals, and the month-end
balance after known standing-order and loan obligations; raise a low-balance notification. Every response
is labelled "Predicted, not exact" (`docs/ml/balance-forecast.md`).

Method by history length (`balance-forecast.md` "Model choice by history length"): 28+ days statsforecast
AutoARIMA with weekly season; 14-27 days seasonal naive; under 14 days naive (last balance). It is fitted
per request on that account's own ledger history; there is no stored trained artifact.

**Out of scope:** credit, affordability or lending decisions; guarantees of future balance; joint-account
co-holders' standing orders (not counted); salary-date regressors (not used).

## Training data

None stored: each forecast fits on the account's own ledger history. The method was **chosen** with a
rolling-origin backtest on synthetic data: `uv run python -m pbip_ml.forecasting.backtest --customers 60
--months 12`, seed 42 (`backtest.py` `--seed` default 42), 59 balance series (data-gen v1), 4 origins per
series 30 days apart, 30-day horizon, 233 forecasts per method (`balance-forecast.md` "Rolling-origin backtest").

## Features

The account's daily balance series only, with past scheduled-engine debits added back to avoid double
counting obligations (`balance-forecast.md` "End-of-month projection"). No demographic data.

## Evaluation (copied from `docs/ml/balance-forecast.md` "Rolling-origin backtest (real run)")

| Method | sMAPE mean | sMAPE median | 80% coverage | 95% coverage |
|---|---|---|---|---|
| AutoARIMA (served) | 10.31% | 8.56% | 86.6% | 96.4% |
| AutoETS | 12.48% | 10.89% | 93.3% | 97.7% |
| Seasonal naive | 15.13% | 14.39% | 85.8% | 94.9% |

Coverage above nominal means the intervals are slightly conservative. Cost: AutoARIMA about 0.9 s per fit on
a 2-CPU laptop; numba compile adds about 2 s to the first forecast per API process; not measured in staging
(`balance-forecast.md` "Limits"). The seasonal-naive and naive branches for short histories have no separate
evaluation on main.

## Limitations

From `balance-forecast.md` "Limits": synthetic data only, so re-run the backtest on real history; the
history is the ledger only; scheduled payments are handled outside the model; salary dates are not
regressors; co-holders' standing orders on joint accounts are not counted. The low-balance alert is computed
on read, so a customer who never opens the forecast is not alerted.

## Fairness

Uses only the account's own balance history and its own scheduled obligations. No demographic or protected
attribute. Outputs inform the customer; they drive no decision about the customer.

## Retraining and promotion

No registry or promotion: the model is refitted on every request. A method or parameter change is a reviewed
code change in `model.py` that must re-run `backtest.py`, update `docs/ml/balance-forecast.md`, and bump
`MODEL_VERSION` (`forecast-<method>-<n>`).
