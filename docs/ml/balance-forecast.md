# Balance forecast (TASK-042)

`GET /api/v1/analytics/forecast?account_id=…&horizon_days=30` projects an owned account's end-of-day
balance with 80% and 95% prediction intervals. Every response carries
`label: "Predicted, not exact"`.

## Model choice by history length
| History (days) | Method | Why |
|---|---|---|
| 28 or more | statsforecast **AutoARIMA**, weekly season | Best in the backtest below |
| Under 28 | Naive (last balance) | Too little history to fit a model honestly; best in the short-history backtest below |

Until `forecast-arima-2` (#384), 14-27 days used weekly seasonal naive. It repeats last week's balance
**levels**, so a one-off debit (Alice: INR 50,000 out around 1 Nov) was projected to swing back to the
old balance every week, and the 30-day point read as "your money comes back".

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

### Short-history backtest (real run, #384)
`uv run python -m pbip_ml.forecasting.backtest --short-history` with seed 42: the same 59 series and 4 origins
per series 30 days apart, but each forecast sees only a fixed 14, 21 or 27-day window before its origin;
30-day horizon, 235 forecasts per method and window.

| Window | Method | sMAPE mean | sMAPE median | 80% coverage | 95% coverage |
|---|---|---|---|---|---|
| 14 days | Seasonal naive (before) | 15.15% | 14.39% | 76.1% | 89.0% |
| 14 days | **Naive (served)** | **12.44%** | **10.89%** | **80.1%** | 85.7% |
| 21 days | Seasonal naive (before) | 15.15% | 14.38% | 74.1% | 87.1% |
| 21 days | **Naive (served)** | **12.53%** | **10.93%** | **81.3%** | 86.9% |
| 27 days | Seasonal naive (before) | 15.11% | 14.38% | 74.3% | 87.7% |
| 27 days | **Naive (served)** | **12.45%** | **10.93%** | **85.0%** | **90.7%** |

Naive has the lower error at every window and an 80% interval near or above nominal, where seasonal naive's
under-covers. Both 95% intervals under-cover (85.7-90.7%), naive's slightly more than seasonal naive's on 14
and 21 days: treat the 95% band of a short-history forecast as narrower than it claims. Two variants that
forecast the daily net flow instead of the level (last balance plus last week's daily changes, with and
without large one-off changes zeroed) were tried in a scratch run and not adopted: the unfiltered one repeats
a one-off debit every week (sMAPE about 37%), the filtered one had a higher mean sMAPE than naive (about
14.2-14.5%) and no interval model.

## End-of-month projection (TASK-043)
The same endpoint combines the model with the payments the bank already knows about:

1. **Known obligations**: every occurrence, from today to the later of the horizon end and the month end,
   of the customer's ACTIVE standing orders that debit the account, and the unpaid instalments of each loan
   whose repayment instruction is ACTIVE (exact amounts; an overdue instalment is dated today because the
   engine retries arrears daily, ADR-0024). A standing-order occurrence before today is not counted: the
   engine records it as missed and does not pay it (ADR-0023). Paused and cancelled instructions are not
   obligations.
2. **Everything else** is forecast by the model above, from the balance history with every past debit of the
   scheduled engine (`transactions.channel = 'SCHEDULED'`, reversed ones excluded) added back. Otherwise last
   month's EMI would be in the history as a pattern *and* this month's EMI would be subtracted: counted twice.
3. `projected(t) = model(t) - scheduled debits added back - obligations due from today to t`. The interval
   bounds move by the same amounts (obligations are known, so they add no uncertainty).

The pure arithmetic is `apps/api/src/pbip_api/modules/analytics/projection.py` (unit-tested). The response
adds `history` (30 days of actual balances), `obligations`, `points[].obligations_minor` and `end_of_month`.
The forecast always reaches the month end, even when `horizon_days` is shorter.

**Low-balance alert.** `end_of_month.low_balance` is true when the predicted (central) month-end balance is at
or below `alert_threshold_minor` = INR 1,000.00 minus the overdraft limit, that is within INR 1,000 of zero, or
of the overdraft limit when the account has one. The endpoint then adds one `PROJECTED_LOW_BALANCE` bell
notification, at most once per user, account and calendar month (a transaction-scoped advisory lock makes it
safe under concurrent reads). It is computed on read, with no background job: a customer who never opens the
forecast is not alerted. The assistant's forecast tool uses the read-only variant and never notifies.

## Limits
* Synthetic data only: real customer behaviour will differ; re-run the backtest on real history.
* Under 28 days the forecast is flat (naive): it shows no weekly pattern and no salary, and its 95% interval
  is slightly narrow (short-history backtest above).
* AutoARIMA costs about 0.9 s per fit on a 2-CPU laptop (AutoETS about 0.2 s); statsforecast's
  numba compile adds about 2 s to the first forecast in each API process. Not measured in staging.
* The history is the ledger only. Scheduled payments are handled outside the model (above); salary dates are not
  used as regressors.
* Only the viewing customer's own instructions are counted: a co-holder's standing orders on a joint account are
  not in the projection.
