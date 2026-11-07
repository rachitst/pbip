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

## Still to measure

- **TASK-102 page-load and login budgets against staging** (deploy guide section 18.1) need the public host
  reachable on 443; on 8 November the Oracle security list did not pass TCP 80/443 to the instance (0 packets at the
  VM), so this run, the Let's Encrypt certificate and the pipeline deploy wait for that rule.
