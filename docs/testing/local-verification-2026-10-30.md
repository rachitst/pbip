# Local verification run, 2026-10-30

Evidence for items that CI does not exercise: the full suite against PostgreSQL and Redis on a
developer machine, a real Celery worker running the nightly analytics task, the offline
segmentation job on seeded data, and analytics latency against TASK-040's 50 ms target.

## Environment

* `main` at `7bd84ec`; stack started with `python scripts/dev.py infra-up`, then
  `docker compose up -d --build --wait` (images rebuilt from `main`).
* Ubuntu laptop, Intel Celeron N4500 (2 cores), Docker 29.9.2, PostgreSQL 16, Redis 7.
* Migrations on the existing development database ran `0002 -> 0003 -> 0004 -> 0005` without error.

## 1. Full test suite against PostgreSQL and Redis

`python scripts/dev.py test`: **566 passed, 8 skipped** (Windows-only runner tests), coverage
**95.99%** (gate 80%).

## 2. Data

`python scripts/dev.py seed`, then six verification customers created through the API: deposits
through `ledger.deposit`, payees added and verified through the API, 18 transfers through
`POST /api/v1/transfers` (14 posted with 201, **4 held for fraud review with 202**), one posted
transfer reversed through `ledger.reverse_transaction`. Payees were aged 48 hours with the same
`UPDATE` the integration helper `age_payee` uses. The data is small: a few entries per account.

## 3. Celery worker: nightly spending materialisation

```text
$ docker compose exec worker celery -A pbip_api.worker.celery_app call pbip.analytics.materialize_monthly_spending
Task pbip.analytics.materialize_monthly_spending[...] received
spending_materialized          account_months=1
Task ...materialize_monthly_spending[...] succeeded in 0.0568s: 1
```

The single account-month is the posting month of the reversed transfer (the reversal lookback from
#171); the stored rows no longer contain the reversed transfer. The default periods (the two
previous closed months) had no activity in this data. `beat` was running and dispatching the
10-second outbox task; the 02:30 UTC analytics schedule was **not** observed (it would require
waiting for it).

## 4. Offline segmentation job

`uv run python scripts/run_segmentation.py --report-file ...`: 9 customers with activity clustered
into 5 segments, Silhouette **0.515**, Davies-Bouldin **0.276**, 9 rows written to
`customer_segments`, exit 0. Two customers are older development-database users with no recent
activity (assigned INACTIVE_SAVER). Nine customers is far too few to judge segment quality; this
proves the job runs end to end, not that the segments are meaningful.

## 5. Analytics latency (TASK-040 target < 50 ms)

300 sequential requests per endpoint after 20 warm-up requests, one customer, client-side wall
clock (includes HTTP and JSON overhead).

| Endpoint | Direct API p50 / p95 / p99 (ms) | Through nginx p50 / p95 / p99 (ms) |
|---|---|---|
| spending, running month | 14.3 / 25.2 / 33.6 | 16.3 / 32.6 / 54.8 |
| spending, closed month (live path) | 15.4 / 23.2 / 29.7 | 16.5 / 23.2 / 30.2 |
| trends | 18.9 / 33.7 / 65.1 | 21.0 / 30.9 / 55.5 |
| health-score | 15.2 / 21.2 / 26.3 | 17.4 / 25.4 / 32.7 |
| segment | 10.4 / 15.5 / 18.8 | 13.3 / 20.1 / 29.3 |

**Reading:** p95 is under 50 ms for every endpoint on this machine; p99 exceeds it for trends and,
through nginx, for the running-month breakdown. **Limits:** tiny dataset (an account with years of
entries was not tested), no concurrency, one laptop. This supports the 50 ms target for small
accounts at low load; it is not evidence for production scale (TASK-078 and TASK-102 own that).
