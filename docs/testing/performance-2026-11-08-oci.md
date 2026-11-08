# Performance results on the Oracle Cloud staging host, 2026-11-08 (TASK-078, TASK-079, TASK-102)

This follows `performance-2026-11-04.md`. It records the first runs on the staging host and the database-pool fix
(ADR-0060, #499) they led to. Every number comes from a run named below; the raw k6 summaries,
`pg_stat_statements` extracts, environment files and invariant reports were copied off the host and are kept with
the operations evidence (they are not committed: they are run artefacts).

## Machine and method

- **Host:** Oracle Cloud Always Free `VM.Standard.A1.Flex`, 2 OCPUs (Ampere Neoverse-N1, 1 thread per core), 12 GB,
  Ubuntu 24.04 aarch64, region `ap-mumbai-1` (deploy guide section 5).
- **Stack under test:** the separate local-mode stack of deploy guide section 18.2 (compose project `pbip`,
  `APP_ENV=local`, `infra/k6/compose.load.yml`, `API_REPLICAS=2`), built on the host from the named commit. The
  staging stack (`pbip-prod`, 7 containers, idle) ran beside it.
- **Load:** `infra/k6/load.js` through nginx TLS on the host (`BASE_URL=https://localhost:8443`), 500 seeded users
  (`scripts/seed_load.py --users 500`), a 30 s ramp then 2 minutes at each step: 50, 100, 200 and 500 virtual users.
  The k6 load generator runs on the same two cores, as the guide prescribes, so the network is not measured and the
  generator competes with the stack for CPU.
- **Checks per step:** k6 thresholds (the NFR budgets: p95 < 500 ms per route group, login < 3 s, errors < 0.5%),
  `pg_stat_statements` reset before and read after, and `scripts/load_invariants.py` (no overdraft, every transaction
  balanced, balances equal their entries, no deadlocks, one idempotency key and one fraud event per transfer).

## Results

p95 in milliseconds, per route group.

**v1.1.0** (`fd2dfbc`):

| VUs | Requests | Errors | Accounts | Analytics | Assistant | Transfers | Login | Refresh |
|---|---|---|---|---|---|---|---|---|
| 50 | 10,815 | 0.00% | 631 | 795 | 1,213 | 1,841 | 1,690 | 833 |
| 100 | 11,598 | 0.00% | 1,826 | 1,842 | 3,112 | 4,006 | 3,108 | 2,414 |
| 200 | 4,293 | 3.80% | 30,026 | 30,039 | 30,002 | 30,002 | 30,001 | 30,002 |
| 500 | 13 | 7.69% | - | - | - | - | 24,036 | - |

**With the admission fix** (#499, `34c1b68`, the same code as `main` after it was merged):

| VUs | Requests | Errors | Accounts | Analytics | Assistant | Transfers | Login | Refresh |
|---|---|---|---|---|---|---|---|---|
| 50 | 10,919 | 0.00% | 631 | 713 | 1,182 | 1,780 | 1,675 | 805 |
| 100 | 11,405 | 0.00% | 1,844 | 1,859 | 2,597 | 3,628 | 3,017 | 2,218 |
| 200 | 12,001 | 0.00% | 4,102 | 4,106 | 5,120 | 5,795 | 5,294 | 4,627 |
| 500 | 11,979 | 0.00% | 10,879 | 11,016 | 11,982 | 12,599 | 12,007 | 11,136 |

**Money invariants:** 0 violations, 0 deadlocks, 0 overdrawn accounts and 0 unbalanced transactions after every step
of both runs; each transfer has exactly one idempotency key and one fraud event.

## What the numbers say

1. **The collapse at 200 VUs was pool starvation, now fixed (TASK-079).** On v1.1.0 the CPU went 73-90% idle during
   the 200-VU step while every route answered after 30 s: the API logged 255
   `QueuePool limit of size 10 overflow 10 reached, connection timed out, timeout 30.00` and nginx 30 s upstream
   timeouts. FastAPI runs each sync dependency, the endpoint and the response validation as separate worker-thread
   calls, and a request keeps its connection between them; with more requests in flight than connections, every
   thread waited for a connection held by a request waiting for a thread. ADR-0060 admits no more requests per
   process than the pool has connections and keeps every request to one connection. With it, 200 and 500 VUs run
   with 0 errors (`test_db_pool_starvation.py` reproduces the cycle on a small pool and fails on v1.1.0).
2. **The p95 budgets are not met on this host, at any step.** Throughput levels at about 80 requests/s from 100 VUs
   upward, and latency grows with the queue: the two cores are saturated by two API replicas, PostgreSQL, Redis,
   the Celery worker and the k6 generator itself (0-3% idle). This is a capacity result for 2 Arm cores shared with
   the load generator, not a code regression: the same suite meets the budgets on 4-vCPU CI runners
   (`performance-2026-11-04.md`, ADR-0045). 500 virtual users now complete without errors, which is the TASK-078
   robustness target, but not within the latency budget.
3. **No slow statement to index.** After the fix the top statement by total time is still
   `SELECT pg_advisory_xact_lock($1)` (the per-user lock, mean 265 ms at 100 VUs and 384 ms at 200), then the
   accounts `SELECT ... FOR UPDATE` (mean 54-74 ms). Every other statement averages under 10 ms. These are lock waits
   behind CPU-starved lock holders; an index would not shorten them, and the lock order is a correctness contract
   (AGENTS.md section 4).

## Assistant first-token latency (NFR API < 500 ms, assistant path)

Measured on the local demo stack (developer laptop: Intel Celeron N4500, 2 cores, 7 GB), through nginx TLS,
`POST /api/v1/assistant/chat/stream`, signed in as the demo customer Alice, one request every 7 s, the same 30
open questions and 10 clear questions in both runs. The "before" run is v1.1.0; the "after" run is `main` at
`ed17756` (ADR-0037 addendum and ADR-0060).

| First token, ms | Before n | p50 | p95 | max | After n | p50 | p95 | max |
|---|---|---|---|---|---|---|---|---|
| All 30 open questions | 30 | 688 | 1,737 | 8,093 | 30 | 364 | 729 | 850 |
| ... routed by Groq | 20 | 746 | 3,094 | 8,093 | 17 | 582 | 754 | 850 |
| ... answered by the rules | 10 | 110 | 139 | 139 | 13 | 52 | 72 | 76 |
| 10 clear questions (rules) | 10 | 61 | 103 | 103 | 10 | 54 | 66 | 66 |

Of the open questions answered by the rules after the change, 10 are the new overview rule and 3 are the
per-customer Groq budget (`RATE_LIMIT_GROQ_PER_MINUTE=5`) falling back; before the change all 10 were budget
fallbacks. The 2.5 s Groq deadline bounds the tail; the Groq path itself (one model round trip) is still above
500 ms at p50, so the NFR is met for the rules path and not for the model path.

### After the budget change (#502, 8 Nov 04:20 UTC)

Same method and question set, demo stack rebuilt from `main` at `bd8e8d6` (`RATE_LIMIT_GROQ_PER_MINUTE` default 10):

| First token, ms | n | p50 | p95 | max |
|---|---|---|---|---|
| All 30 open questions | 30 | 500 | 724 | 757 |
| ... routed by Groq | 20 | 626 | 728 | 757 |
| ... answered by the rules (the overview rule) | 10 | 50 | 75 | 75 |
| 10 clear questions (rules) | 10 | 52 | 90 | 90 |

No question ran out of budget at 7 s spacing (about 8.5 questions a minute), so the 3 questions that fell back to the
help text in the previous run are now routed by the model: the open-question p50 rises (364 to 500 ms) because three
generic answers became tool answers, and the worst case falls (850 to 757 ms). Through the stack's nginx a used
budget was already fast (the API container does not reach a host Ollama); on a host where Ollama is reachable the
budget path took about 4.2 s and now ends in the rules at once (ADR-0037 addendum, unit and integration tests).

## Second pass, 8 November 03:30-04:30 UTC (HTTPS reachable)

TCP 80/443 reached the instance from 03:25 UTC; the Let's Encrypt certificate was issued at 03:26 and the Deploy
workflow deployed v1.1.1 with its smoke test (run 42442978136, 6/6 checks over HTTPS from the GitHub runner).

### TASK-102: page-load and login budgets against `https://pbip.duckdns.org`

`apps/web/e2e/perf.spec.ts`, Chromium, from the developer laptop (round trip to the instance about 9 ms), against the
staging stack itself (v1.1.1, two API replicas, Let's Encrypt TLS), one freshly registered customer per run, no
other load. Three runs, 03:40-03:45 UTC:

| Measurement (budget) | Run 1 | Run 2 | Run 3 | Worst |
|---|---|---|---|---|
| `/` signed out, empty cache (2 s) | 1,105 ms | 1,044 ms | 1,357 ms | 1,357 ms |
| Login, "Sign in" to "My accounts" (3 s) | 675 ms | 796 ms | 868 ms | 868 ms |
| `/accounts` reload (2 s) | 666 ms | 482 ms | 620 ms | 666 ms |
| `/insights` reload (2 s) | 947 ms | 867 ms | 1,015 ms | 1,015 ms |
| `/profile` reload (2 s) | 491 ms | 464 ms | 582 ms | 582 ms |

Every budget is met on staging, with at least 32% headroom. Three earlier runs with the list reporter also passed.

### TASK-078/079: the k6 suite with the load generator off the host

The first pass ran k6 on the VM, so the generator competed with the stack for the two cores. This pass ran k6 1.3.0
on the developer laptop (Intel Celeron N4500, 2 cores; 27-35% busy during the runs, so not the limit) against the
separate local-mode stack on the VM (project `pbip`, v1.1.1, `API_REPLICAS=2`, 500 seeded users, the load-test rate
limits), over HTTPS through an SSH tunnel to its nginx (only TCP 22/80/443 reach the instance; sshd used 4.6% of a
core at 200 VUs). It is not the `pbip-prod` stack: `scripts/seed_load.py` and the load-test rate limits are refused or
forbidden on a staging database (deploy guide section 18.2), and staging cannot fund accounts. The staging stack
(16 containers including the monitoring stack, idle) ran alongside. 2-minute steady state per step.

| VUs | Requests (k6 rate, whole run incl. setup) | HTTP errors | p95 accounts / analytics / assistant / transfers | p95 login / refresh | VM CPU busy |
|---|---|---|---|---|---|
| 50 | 10,158 (39.7/s) | 0.00% | 720 / 860 / 1,307 / 1,922 ms | 1,780 / 963 ms | 86% |
| 100 | 10,897 (42.6/s) | 0.00% | 1,893 / 1,941 / 2,879 / 3,836 ms | 3,220 / 2,195 ms | 87% |
| 200 | 10,629 (41.1/s) | 0.00% | 5,676 / 5,739 / 6,663 / 7,336 ms | 6,812 / 6,065 ms | 89% |
| 500 | 11,420 (44.2/s) | 0.00% | 12,231 / 12,115 / 13,157 / 13,978 ms | 13,007 / 12,421 ms | 91% |

`unexpected_responses` was 0 and `scripts/load_invariants.py check` passed after every step (no overdraft, every
transaction balanced, no deadlock). In `pg_stat_statements` the per-user `pg_advisory_xact_lock` is again the top
statement by total time (mean 29 ms at 50 VUs, 288 ms at 100, 335 ms at 200, 406 ms at 500); every insert averages
under 7 ms. The two uvicorn processes used 72% and 60% of a core each at 200 VUs (`pidstat`).

What this shows:

1. **Moving the generator off the host did not raise throughput.** Each step completed 10,200-11,400 requests, against
   10,900-12,000 in the first pass with the generator on the host (about 70-80 requests/s over the 150 s of ramp and
   steady state); the first pass ran before the monitoring stack was started. The limit is the CPU available to the
   two API processes, PostgreSQL and the monitoring stack, not k6.
2. **The 500 ms p95 at 500 users is not met on this host and cannot be with this shape.** At 500 VUs with a 1 s think
   time the suite offers several hundred requests/s; the host completes about 75/s, so the queue sets the latency.
   This is a hardware bound of the Always Free 2-OCPU shape. The repository evidence for the budget is the CI run on
   4 vCPUs at **50 VUs** (`performance-2026-11-04.md`: p95 61-73 / 73-100 / 140-190 / 287-347 ms, 0 errors); no run
   on any hardware has met the budget at 500 VUs.
3. **Correctness held at every step.** The error-rate target (< 0.5%) and the money invariants are met at 500 VUs over
   more than 10,000 requests; the TASK-079 pool fix (ADR-0060) is what makes that true on this host.
4. **The advisory-lock wait grows with the queue, not with contention between customers.** Each VU signs in as its
   own seeded user; the lock is held for the length of one transfer transaction, and that transaction is slow because
   its process is CPU-starved. It is the lock-order contract (AGENTS.md section 4), not an index or pool problem.

The trained models were not loaded in these perf-stack runs (its `models/` directories were empty, created by a
bind mount), so fraud scoring used the rules; CI runs the suite with the hybrid scorer.

### TASK-089 criterion 3 on the deployed stack

A Playwright sweep (one registered customer, laptop to `https://pbip.duckdns.org`) visited the 12 customer pages
(accounts, payees, bills, scheduled, statements, insights, loans, wealth, compare, invest, insurance, profile) and the
not-found page: 0 console errors, 0 uncaught page errors, 0 responses >= 500. Staff pages cannot be checked on staging:
there is no supported staff bootstrap there (deploy guide section 14).
