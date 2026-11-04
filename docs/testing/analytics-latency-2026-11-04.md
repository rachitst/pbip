# Analytics latency under concurrency, 2026-11-04 (TASK-040 AC3, issue #175)

TASK-040 AC3 says the analytics endpoints respond in under 50 ms. Sequentially they do: p95 12-19 ms
(`local-verification-2026-10-30.md`). Under concurrency they do not: the k6 suite (50 VUs, about 73 requests/s,
two API replicas, 4-vCPU CI runner) gave an analytics p95 of 73 ms (Xeon 8370C) and 100 ms (EPYC 7763)
(`performance-2026-11-04.md`). This page records one optimisation pass and its honest result.

## Where the time goes

Measured endpoint by endpoint, sequentially, on the 2-CPU reference laptop (Celeron N4500, shared with other
sessions), with Alice from `scripts/seed_dev.py` (three months of history) on a scratch database.

| Endpoint | SQL statements | What dominates |
|---|---|---|
| `spending` | 4 (token check, customer, account, month's debits) | the shared authentication base |
| `trends` | 6 (+ stored previous month, + live fallback when nothing is stored) | the shared base |
| `health-score` | 5 (+ owned account ids, 12-month entries window) | the shared base |
| `forecast` | 7 | **the AutoARIMA fit on every request**: p50 1.6-2.0 s in process, 0.5-1.0 s over HTTP |

Every authenticated endpoint pays the same base: JWT verification, the token-version check and the customer
lookup (2 statements), the pool pre-ping, BEGIN and ROLLBACK, middleware and serialisation. That is about
7-9 ms of server CPU and 5 database round trips, which is what `GET /api/v1/accounts` costs. The analytics queries
themselves are small: the month's debits run in 0.11 ms on 2M entries (`performance-2026-11-04.md`).

Under concurrency, the 4-vCPU CI runs show the API process CPU-bound (one core per replica) while PostgreSQL waits
on the API (`ClientRead`) and lock waits are rare. So analytics p95 under load is CPU queueing for every request
type, and it tracks the accounts p95 in the same run (61-73 ms) plus the analytics work.

## What this pass changed (ADR-0054)

- **Forecast:** the model fit is a pure function of the balance history up to yesterday, the first forecast day and
  the horizon, so it changes once a day. The result is now kept in process under (customer id, account id) and a
  SHA-256 of exactly those inputs. Today's balance, scheduled obligations and the low-balance alert are still
  computed per request. Tests: `test_forecast_cache.py`.
- **Not changed:** caching the other analytics responses was rejected. Under the k6 mix each user transfers about
  every 7 s and requests a given analytics endpoint about every 45 s, so a version-checked response cache would
  almost never hit there. Stored monthly aggregates are already used for closed months (`monthly_category_spending`).
  Pool size: the CI probe showed no pool wait (`performance-2026-11-04.md`), so a bigger pool would not help.

## Results

**Sequential, real HTTP, one uvicorn process, main vs this change.** Each run started only when the 1-minute load
average was below 4. 2 warm-up requests, then 20 requests per endpoint. p50 / p95 in ms.

| Endpoint | main, load 3.73 | main, load 3.80 | change, load 3.59 | change, load 3.54 |
|---|---|---|---|---|
| forecast | 975 / 1,219 | 530 / 999 | 33 / 37 | 21 / 44 |
| health-score | 17 / 30 | 30 / 87 | 24 / 49 | 17 / 20 |
| spending | 19 / 25 | 13 / 15 | 26 / 30 | 17 / 32 |
| trends | 51 / 73 | 17 / 20 | 33 / 46 | 18 / 20 |

The three unchanged endpoints vary between runs by as much as they differ between variants: that is the laptop's
noise at n = 20. The forecast is 15-45 times faster at the median for every repeat view on the same day.

**k6 `infra/k6/load.js` on the laptop**, one uvicorn process, scratch database with 100 seeded load users. The
coordinator's rule for this machine is a modest VU count, so this is **10 VUs for 1 minute**, not 50. Runs were
interleaved; each started at a load below 4, but k6 itself raised it while running.

| Run | Requests | Errors | p50 / p95 accounts | p50 / p95 analytics | Load median (max) |
|---|---|---|---|---|---|
| main 1 | 1,758 | 0 | 21 / 67 | 30 / 75 | 3.6 (4.2) |
| change 1 | 1,753 | 0 | 28 / 87 | 33 / 89 | 4.1 (4.7) |
| change 2 | 1,749 | 0 | 22 / 64 | 37 / 107 | 4.7 (6.0) |
| main 2 | 1,729 | 0 | 30 / 79 | 43 / 101 | 5.4 (6.6) |

No difference beyond noise, as expected. The k6 load users are created on the day of the run, so the forecast
answers `INSUFFICIENT_HISTORY` without fitting, and the other analytics endpoints are unchanged. **The k6 suite does
not exercise the forecast fit**, which was the analytics path that cost the most. That is a gap in the suite, not
evidence that the fit was cheap.

**CI Performance workflow (50 VUs, 2 min, two API replicas, nginx TLS, 4 vCPU), dispatched 2026-11-04.** GitHub
assigned the two runs different CPU classes, so they show the runner more than the change:

| Run | Commit | CPU | Requests | Errors | p95 accounts / analytics / assistant / transfers |
|---|---|---|---|---|---|
| 41784971903 | this change | Xeon Platinum 8573C | 13,853 | 0 | 59 / 76 / 152 / 297 ms |
| 41785405003 | main (0c029f2) | EPYC 9V74 | 14,307 | 0 | 30 / 41 / 69 / 175 ms |

In both runs analytics p95 is the accounts p95 plus 11-17 ms. On the fast EPYC 9V74 class, main already meets
< 50 ms under concurrency (41 ms); on the slower classes (Xeon 8370C/8573C, EPYC 7763) it does not (73-100 ms).

An earlier 50-VU pair on this laptop ran at a load of 20-37 (analytics p95 2.0-2.2 s for both variants). The
machine was overloaded, so those numbers are not usable and are not reported as results.

## Verdict

- **Sequential: met.** Every analytics endpoint now meets < 50 ms p95 at a load below 4, including a repeat forecast.
  A forecast's first request of the day per account and API process still fits the model (0.5-2 s on this laptop).
- **Under concurrency: met only on the fast CI CPU class** (EPYC 9V74: 41 ms), not on the slower ones (73-100 ms), and
  this change does not move it. The measured floor under load is the shared
  per-request base that `GET /api/v1/accounts` also pays. In the same runs accounts p95 was 61-73 ms on CI (50 VUs)
  and 64-87 ms on the laptop (10 VUs). The analytics group cannot be faster than that floor while the API process is
  CPU-bound. Reaching < 50 ms p95 at 50 VUs needs more API CPU (replicas or faster cores; ADR-0045) or a lower base
  cost for every endpoint, for example merging the token-version check with the customer lookup or dropping the pool
  pre-ping. Those are platform and authentication changes for their own review, not an analytics optimisation.
