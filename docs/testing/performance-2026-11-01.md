# Performance and load results, 2026-11-01 (TASK-078, TASK-102, TASK-079)

**Machine: the reference laptop, not staging.** Intel Celeron N4500 (2 cores, 1.1 GHz), 7 GB RAM, Linux. k6, the
browser, nginx, the API, PostgreSQL, Redis, Celery and the monitoring stack all ran on it, and it was shared with
other development sessions, so the 1-minute load average was **9 to 27** during the runs (recorded per run below).
These numbers show how the system behaves when CPU-starved. They are not a capacity statement. Staging measurements
wait for a host: #198 (first deployment) and #101 (TASK-078 on staging). The nightly `.github/workflows/perf.yml`
job runs the same suite on a GitHub-hosted runner and records that runner's hardware with each result.

**Stack under test:** `main` at `98b8603` plus the observability (TASK-069/103) and load-suite branches, as a separate
compose project. Traffic went through nginx TLS (`https://localhost:18443`) to one uvicorn process, with TLS to
PostgreSQL (`verify-full`), the hybrid fraud scorer on (`hybrid-lgbm-iforest-1`), the load-test rate-limit overlay,
and `pg_stat_statements` enabled. Load: `infra/k6/load.js`, 200 seeded users, 50 VUs, 30 s ramp then 3 min steady,
1 s ± 50% think time, k6 1.3.0.

## TASK-078: load suite results

| Run | Load avg start → end | Requests (rate) | HTTP errors | p95 accounts / analytics / assistant / transfers | p95 login / refresh | Transfers (201/202/403) |
|---|---|---|---|---|---|---|
| 2 (indexes absent; forecast 422 still counted as an error, script fixed afterwards) | 9.5 → 26.8 | 7,056 (22.1/s) | 1.31% (all 93 = forecast's documented 422) | 1.52 / 1.66 / 2.42 / 3.36 s | 2.97 / 2.18 s | 561 |
| A (TASK-079 indexes dropped) | 12.2 → 19.2 | 6,404 (20.4/s) | **0.00%** | 1.79 / 1.94 / 2.76 / 3.75 s | 3.23 / 2.58 s | 546 |
| B (TASK-079 indexes present) | 10.6 → 18.2 | 5,592 (16.3/s) | **0.00%** | 2.38 / 2.48 / 3.58 / 5.00 s | 4.69 / 3.69 s | 474 |

Run 1 (not shown) had two script bugs: analytics called without `account_id`, and a byte-for-byte replay comparison.
Both were fixed before runs 2, A and B.

**Money and concurrency invariants** (`scripts/load_invariants.py`, after every run): **0 deadlocks**, 0 overdrawn
accounts, 0 unbalanced transactions, 0 balance/entry mismatches, and money conserved in the load population
(20,000,000,000 minor units before and after). Across the runs, 2,199 transfers went through the ledger
concurrently, and every idempotent replay returned the same transfer marked `Idempotent-Replayed` (checks 100% in A
and B).

**Verdict against the TASK-078 criteria:** traffic mix met (70/20/10). Pool and advisory-lock throughput validated:
no deadlock, no overdraft, no lock timeout. Summary report with p50/p95/p99 and error rate: met (k6 summary; p50 and
p99 are in the logs). **10,000 requests in one run: not met** (5,592-7,056 per run on this machine; 19,052 across
runs 2, A and B). **"p95 within budget" (DoD): not met on this laptop**: every route group is 1.5-5 s at 50 VUs.
Error rate < 0.5%: met (0.00%).

## TASK-079: profiling, indexes and pool

**Profile under load** (`pg_stat_statements`, run B; run 1 was similar):

| Statement | Calls | Mean | Max | What it is |
|---|---|---|---|---|
| `SELECT accounts … FOR UPDATE` (transfer) | 622 | 37.3 ms | 1,547 ms | **Row-lock wait**: user *i* pays *i+1*, so neighbours lock the same account |
| `pg_advisory_xact_lock` | 652 | 32.5 ms | 700 ms | Per-user lock (AGENTS lock order); waits stretched by CPU starvation |
| `fraud_events` features for the customer | 474 | 0.54 ms (A: **4.46 ms**) | 26 ms (A: 86 ms) | Read on **every** scored payment; was a sequential scan |
| account reads | 3,161 | 0.62 ms | 171 ms | Primary-key and index lookups |

**Indexes added** (migration 0012): `fraud_events (customer_id, created_at)`; `refresh_tokens (user_id)`
(revoke-all-sessions; unindexed foreign key); `transactions (payee_id)` (payee deletion check; unindexed foreign key).

| Evidence | Without index | With index |
|---|---|---|
| Per-transfer fraud-feature query under load, ~2,000 `fraud_events` rows (runs A and B) | mean 4.46 ms | mean 0.54 ms (8.3×) |
| Same predicate at 500,000 rows and 10,000 customers (scratch database on the same server, load avg ~14) | 76.6 ms per lookup, Parallel Seq Scan; 200 lookups 19.2 s | 0.26 ms, Bitmap Index Scan; 200 lookups 35.6 ms |

End-to-end p95 did **not** improve between A and B (B was slower). With the CPU saturated, run-to-run noise from
other sessions is larger than the index's effect at this table size. The index matters as `fraud_events` grows,
since it gets one row per scored payment.

**Pool.** Peak 23 connections to `pbip`. All 20 of the API's pool connections (10 + 10 overflow) were checked out,
but **20 were idle in transaction and 4 active**: requests held a connection while the CPU-bound Python process
worked. More connections would not help, so the defaults stay (10/10/30 s). They are now settings
(`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT_SECONDS`, validated). The lever is more API processes and
CPUs. Running several uvicorn workers needs Prometheus multiprocess mode for `/metrics`, a follow-up for the staging
host.

**"p95 database query time < 15 ms under 200 concurrent connections": not met / not measurable here.**
`max_connections` is 100, and one API process holds at most 20. Index and primary-key reads stay under 1 ms mean.
The two lock-wait statements exceed 15 ms mean under this CPU starvation.

## TASK-102: page load and login (browser)

`apps/web/e2e/perf.spec.ts`, Chromium, `https://localhost:18443`, one user, no load test running (load avg 9-13):

| Measurement | Result | Budget |
|---|---|---|
| Sign-in page `/`, fresh context (empty cache), until the heading is visible | 1,665 ms (navigation load event 1,059 ms) | < 2 s ✅ |
| Login: press "Sign in" → accounts page heading | 2,102 ms | < 3 s ✅ |
| `/accounts` full reload (session restored from the cookie, data fetched) | 942 ms | < 2 s ✅ |
| `/insights` full reload | 1,288 ms | < 2 s ✅ |
| `/profile` full reload | 913 ms | < 2 s ✅ |

Under the 50-VU load, API login p95 was 2.97-4.69 s (above 3 s in run B).

## Other measured costs

* ADR-0021 access-token check: median 1.9 ms, p95 4.2 ms per request (3,000 calls, shared PostgreSQL, load avg ~6.5).

## Not verified

* Any number on staging hardware (#101, #198), and 500 VUs (TASK-078 outcome): 50 VUs already saturate this laptop.
* The nightly `perf.yml` run: `workflow_dispatch` works only once the workflow is on `main`. Its first result will
  be linked here.
