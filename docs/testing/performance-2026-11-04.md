# Performance results, 2026-11-04 (TASK-078, TASK-079, TASK-102)

This follows `performance-2026-11-01.md`. It covers why the nightly `Performance` workflow failed on most `main`
commits, the fix (ADR-0045), and the database evidence for TASK-079. Every number comes from a CI artifact or a
local run named below. The machine is stated for each one.

## Why the workflow failed

The k6 suite (`infra/k6/load.js`: 50 VUs, 100 users, a 2-minute steady state, about 73 requests/s through nginx TLS)
failed on 16 of 22 `main` runs between 2 and 3 November. The result followed the runner's CPU, not the commit:

| Runner CPU (4 vCPU) | Runs | Transfers p95 | Accounts p95 | Result |
|---|---|---|---|---|
| AMD EPYC 9V45 / 9V74 | 6 | 136-262 ms (one run 786 ms) | 30-66 ms | 5 pass |
| AMD EPYC 7763, Intel Xeon 8370C | 16 | 603-924 ms | 156-270 ms | 0 pass |

A probe run (`diag/perf-probe`, run 41703597729) sampled the containers' CPU, PostgreSQL's wait events and the API's
access log:

- The single API process ran at about 100-120% CPU (one core). The host was still 20-45% idle and PostgreSQL used
  19-30%.
- PostgreSQL was almost never waiting on a lock. Out of about 400 non-idle samples per run, 0-7 were waits on the
  audit-chain advisory lock and 0-6 were row-lock waits; the rest were `ClientRead`, waiting on the API.
- The API's own access log showed the same p95 as k6.

That rules out a slow query, the connection pool, rate limiting (the load overlay already raises the per-IP limits)
and lock contention. Two variants changed nothing on the same CPU class:

- flushing pending writes before the audit-chain lock (transfers p95 825 ms);
- seeding payees so that no two active senders share an account.

Two API processes passed on an EPYC 7763: transfers p95 353 ms.

## Fix and result (ADR-0045)

Production now runs two API replicas, and the workflow measures that topology (`API_REPLICAS=2`). No threshold
changed. Results on branch `fix/performance-workflow`, both on the slower CPU class:

| Run | CPU | Requests | HTTP errors | p95 accounts / analytics / assistant / transfers | p95 login / refresh | Transfers p50 / p99 |
|---|---|---|---|---|---|---|
| 41707736252 | Xeon 8370C | 13,773 | 0.00% | 61 / 73 / 140 / 287 ms | 340 / 97 ms | 102 / 378 ms |
| 41709572444 | EPYC 7763 | 13,954 | 0.00% | 73 / 100 / 190 / 347 ms | 406 / 140 ms | 110 / 506 ms |

Money invariants after both runs: 0 violations, 0 deadlocks, 0 overdrawn accounts and every transaction balanced.
There were 1,332 and 1,214 concurrent transfers, each with exactly one idempotency key and one fraud event.

Page-load budgets in the same runs (Chromium, one user, after the load test):

| Measurement | 41707736252 | 41709572444 | Budget |
|---|---|---|---|
| `/` (fresh context) | 268 ms | 292 ms | 2 s |
| login → accounts page | 251 ms | 277 ms | 3 s |
| `/accounts` | 138 ms | 120 ms | 2 s |
| `/insights` | 449 ms | 451 ms | 2 s |
| `/profile` | 111 ms | 110 ms | 2 s |

## TASK-079: hot queries at realistic size

**Test database.** A throwaway PostgreSQL 16 container, never the demo database. It held the 100 seeded load users,
10,000 more customers with an account each, and 180 days of history: 1,000,000 posted transfers, 2,000,000 ledger
entries, 1,000,000 fraud events and 300,000 security events. The history rows were bulk-inserted with triggers off,
which is only acceptable in a disposable benchmark database. The query texts were captured with
`pg_stat_statements` from real API calls.

**EXPLAIN ANALYZE** (CI runner, Xeon 8370C, run 41713131717): every hot query uses an index.

| Query (where it runs) | Plan | Time |
|---|---|---|
| Transactions page, 26 rows (accounts) | Index Scan Backward `ix_transaction_entries_account_id_seq` + `pk_transactions` | 0.44 ms |
| Customer's debits in the last 30 days (fraud features, every transfer) | `ix_account_holders_customer_id` + Bitmap Index Scan `ix_transactions_debit_account_id_created_at` + `ix_fraud_events_transaction_id` | 0.43 ms |
| Today's total (daily limit, every transfer) | Index Scan `ix_transactions_initiated_by_user_id_created_at` | 0.08 ms |
| Credits in 24 h from other customers (mule rule) | Index Scan `ix_transactions_credit_account_id_created_at` | 0.10 ms |
| Customer's fraud events, 7 days | Index Scan `ix_fraud_events_customer_id_created_at` | 0.05 ms |
| Failed sign-ins in 24 h | Index Scan `ix_security_events_user_id_created_at` | 0.09 ms |
| Month's debits (analytics spending) | Index Scan `ix_transaction_entries_account_id_created_at` | 0.11 ms |
| 90-day balance history (forecast) | Bitmap Index Scan `ix_transaction_entries_account_id_created_at` | 0.20 ms |
| Lock two accounts (transfer) | Bitmap Index Scan `pk_accounts` | 0.20 ms |

The composite indexes these plans use (transactions by account and time, entries by account and time/sequence, fraud
events by customer and time) were added by migration 0012 and earlier. No query needed a new index, so this work adds
no migration.

**Under load** (`pg_stat_statements` in the green runs above): the slowest statement by mean time was the transfer's
`SELECT accounts ... FOR UPDATE` at 1.20 ms. Its 205 ms maximum is a row-lock wait. Every other statement averaged
under 0.7 ms.

**200 concurrent connections** (`pgbench`, 200 clients, 4 threads, 60 s per mode, the five hot queries weighted
30/30/15/15/10 against random accounts out of 10,000). The latency is measured by the client, so it includes the
round trip and, with `-R`, pgbench's own scheduling delay.

| Mode (CI, Xeon 8370C, idle runner) | Throughput | p50 | p95 | p99 | Failed |
|---|---|---|---|---|---|
| 400 queries/s over 200 connections | 397/s | 0.8 ms | **1.7 ms** | 5.1 ms | 0 |
| 1,000 queries/s over 200 connections | 992/s | 0.8 ms | **1.8 ms** | 48.6 ms | 0 |
| Unthrottled (saturation, closed loop) | 4,078/s | 31.3 ms | 79.0 ms | 119.0 ms | 0 |

At 400 and 1,000 queries/s over 200 connections, the p95 stays under the 15 ms target. That is the usual load:
the k6 suite sends about 400 statements/s (`pg_stat_statements` call counts over the run). The unthrottled row shows the saturation point of 4 vCPUs shared by
pgbench and PostgreSQL. Past that point latency is queueing, which is why it is not the measure for the 15 ms
criterion.

**Pool.** No change. Each API replica holds at most 10 + 10 connections, so two replicas plus the worker stay well
under `max_connections` (100).

**The same pgbench on the reference laptop.** 2-CPU Celeron N4500 shared with other sessions, 1-minute load average
21-36: at 400 queries/s over 200 connections, p50 was 2.2 ms and p95 553 ms, which includes pgbench's scheduling
delay. Server-side means were 0.3-11 ms. The laptop cannot run 200 clients without CPU starvation, so the 15 ms
criterion is evidenced by the idle CI runner above, not by this machine.

## TASK-078: 10,000+ requests on the reference laptop

**Machine and stack.** Intel Celeron N4500 (2 cores, 1.10 GHz) with 7 GB RAM, shared with other sessions. The stack
was not the demo stack: the demo database has no load users and runs the default login limits. Instead it was a
throwaway PostgreSQL 16 with the volume described above, Redis db 7, two uvicorn processes (the production topology,
ADR-0045) behind a local nginx, the hybrid fraud scorer, and raised login and assistant limits. k6 1.3.0 ran
`infra/k6/load.js` with 30 VUs, 1 s ± 50% think time, 100 users and a 6-minute steady state, on the same machine.
The window was 04 Nov, 02:37:50-02:45:19 IST. The 1-minute load average was 5.96 at the start, with a median of
17.9 and a maximum of 19.7.

| Requests (rate) | HTTP errors | Unexpected statuses | p50 / p95 / p99 all requests | p95 accounts / analytics / assistant / transfers | p95 login / refresh |
|---|---|---|---|---|---|
| 14,045 (31.4/s) | 0.00% (1 of 14,045) | 0 | 206 / 1,301 / 1,855 ms | 539 / 633 / 1,095 / 1,921 ms | 1,958 / 949 ms |

**Money invariants** (`load_invariants.py`): 0 deadlocks, 0 overdrawn accounts and 0 unbalanced transactions. The
run made 1,273 concurrent transfers, each with exactly one idempotency key and one fraud event. The script also
reports 10,000 balance mismatches, but these were already in the snapshot taken before the run: they are the
bulk-inserted history rows of the extra accounts, not something the run caused.

**Verdict on this laptop:** the error rate and the invariants are met over more than 10,000 requests. Login p95 is
under 3 s. The 500 ms p95 route budgets are **not met** on this CPU-starved machine; they are met with two replicas
on the CI runners (above).

**Bug found by the long run.** An earlier 16-minute run (33,132 requests) showed 11.17% errors. All of them were
401s from minute 15 on: `setup()` signed each user in once, and access tokens live 15 minutes. `load.js` now has
each VU sign in again before its token is 10 minutes old (`TOKEN_MAX_AGE_SECONDS`), as the web app renews its
session. The regression test is `test_k6_load_suite_renews_tokens_before_the_access_ttl`. The 2-minute CI runs
never reached the TTL.

## Two wall-clock tests moved to benchmarks (#317 item 2)

`test_scoring_stays_fast_with_1000_near_threshold_inbound_transfers` (p95 < 100 ms) and
`test_copy_loads_1000_customers_in_under_15_seconds` (< 15 s) failed on busy 2-CPU machines. As in #379 and #412,
the tests now assert cost and shape (`test_scoring_cost_does_not_grow_with_1000_near_threshold_inbound_transfers`:
the same SQL statements for 1,000 near-threshold credits as for 3, at most 20 rows per statement, index scans;
`test_copy_loads_1000_customers_with_one_copy_per_table`: one binary COPY per table, ANALYZE before the big COPY, no
row-level trigger). The time is measured by two scripts, on a scratch database only (name `pbip_test*` or
`pbip_bench*`):

```bash
uv run python scripts/benchmark_structuring_scoring.py --database-url "$BENCH_DATABASE_URL" --calls 200
uv run python scripts/benchmark_synthetic_load.py --database-url "$BENCH_DATABASE_URL" --runs 3
```

Measured again on 4 Nov 2026 on the 2-CPU Celeron team laptop, one job at a time, each run started at a 1-minute
load average below 4 (the runs themselves push it a little higher):

| Benchmark | Load average (1 / 5 / 15 min) | Result | Budget |
|---|---|---|---|
| COPY load, seed 42, 1,000 customers × 12 months (391,800 transactions), 3 fresh loads (generation, 13.2 s, not timed) | 3.87 / 4.33 / 5.47 before, 4.86 / 4.52 / 5.50 after | 7.49 / 8.72 / 8.69 s (median 8.69 s), 3 of 3 within | < 15 s |
| Rules scoring with 1,000 near-threshold inbound transfers, 200 calls after 3 warm-up | 3.44 / 4.13 / 5.26 before, 3.48 / 4.12 / 5.25 after | p50 26.4, p95 45.1, p99 52.7, max 58.3 ms | p95 < 100 ms |

Both are within budget. An earlier pair taken with other jobs running (load 7-8.4) gave a COPY median of 13.97 s and a
scoring p95 of 176.3 ms; that was the CPU, not the query (the cost test shows the work does not grow with the number
of credits), and those numbers are not used. A scoring run that started at load 4.54 (p95 77.0 ms) was discarded.

## The mule-scoring test moved to the benchmark too; hang guards for two long unit tests

`test_scoring_stays_fast_with_1000_inbound_transfers` (`test_fraud_mule.py`, p95 < 100 ms over 40 calls) failed at
load 8 with 103 ms. It is now `test_scoring_cost_does_not_grow_with_1000_inbound_transfers`: 1,000 inbound
transfers cost the same SQL statements as 3, no statement returns more than 20 rows, the two inbound look-ups are
index scans on `ix_transactions_credit_account_id_created_at` and the repeat-episode count on
`ix_transactions_debit_account_id_created_at` (EXPLAIN on fresh statistics), and MULE_PASS_THROUGH fires on both
with the same total, count, 90% ratio and 20 capped ids. Breaking the scorer locally failed it each time: no
`LIMIT` on the id list, an unindexable predicate on the credit or on the debit side, and an extra query per inbound
row. The time is measured by the structuring benchmark's mule mode, on a scratch database only:

```bash
uv run python scripts/benchmark_structuring_scoring.py --database-url "$BENCH_DATABASE_URL" --rule mule --calls 200
```

Measured on 4 Nov 2026 on the 2-CPU Celeron team laptop, at a low load, one job at a time:

| Benchmark | Load average (1 / 5 / 15 min) | Result | Budget |
|---|---|---|---|
| Rules scoring with 1,000 inbound transfers (MULE_PASS_THROUGH fires), 200 calls after 3 warm-up | 1.21 / 1.88 / 2.34 before, 1.19 / 1.87 / 2.33 after | p50 23.0, p95 29.2, p99 35.3, max 109.5 ms | p95 < 100 ms |
| Rules scoring with 1,000 near-threshold inbound transfers (STRUCTURING fires), same run settings | 1.16 / 1.82 / 2.31 before, 1.15 / 1.81 / 2.31 after | p50 24.4, p95 27.5, p99 33.7, max 71.5 ms | p95 < 100 ms |

Two unit tests kept reaching pytest-timeout's 60 s default under load, and are legitimately long, so they carry a
per-test `@pytest.mark.timeout(240)` as a hang guard, not a speed bar:
`test_same_seed_reproduces_the_inputs_and_every_operation_is_fuzzed_in_both_modes` (three complete fuzz passes over
the contract, 11-14 s idle) and `test_the_served_scorer_uses_the_compiled_forest_and_decides_identically` (model
training plus 300 single-row scikit-learn scorings, about 10 s idle). The identical trained model that
`test_fraud_compiled_forest.py` and `test_fraud_inference.py` each trained is now one session fixture
(`ml/tests/conftest.py`), which saves one training (about 5 s idle) per run. No other unit test took over 30 s
(`pytest -m unit --durations=15` at load 1.0-1.9: the slowest was 12.8 s).
