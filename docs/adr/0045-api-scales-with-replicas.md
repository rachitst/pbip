# ADR 0045: The API scales with replicas; production and the load suite run two
- Status: Accepted (under owner's standing approval, 2026-11-04)
- Date: 2026-11-04
- Deciders: Architecture Team (issues #101 TASK-078, #102 TASK-079, #185 TASK-102; failing `Performance` workflow)

## Context
The nightly `Performance` workflow (k6 `infra/k6/load.js`, 50 VUs, about 73 requests/s through nginx TLS) failed on
most `main` commits since 2 Nov and passed occasionally. The artifacts of 22 runs show that the outcome follows the
runner's CPU, not the commit:

| Runner CPU (4 vCPU) | Runs | Transfers p95 | Accounts p95 | Result |
|---|---|---|---|---|
| AMD EPYC 9V45 / 9V74 | 6 | 136-262 ms (one 9V74 run: 786 ms) | 30-66 ms (that run: 220 ms) | pass (5 of 6) |
| AMD EPYC 7763, Intel Xeon 8370C | 16 | 603-924 ms | 156-270 ms | fail (all 16) |

Every route group slows down 4-6x together on the slower CPUs, which is queueing, not one slow endpoint. A probe run
with resource sampling (branch `diag/perf-probe`, run 41703597729) showed where the queue is:

- the API container used **~100-120% CPU** (one core: one uvicorn process, Python's GIL) while the 4-vCPU host was
  still **20-45% idle** and PostgreSQL used 19-30%;
- PostgreSQL backends were almost always waiting for the client (`ClientRead`: a transaction open while the API
  process works), with **no** meaningful lock waits: 0-7 samples of the audit-chain advisory lock, 0-6 of row
  locks, out of ~400 non-idle samples;
- the API's own access log gave the same p95 as k6, so the time is spent inside the process, not in nginx.

Two hypotheses were tested on the same runner class and rejected: flushing pending writes before the audit-chain
lock (transfers p95 825 ms, EPYC 7763) and seeding payees so that no two active senders share an account
(no change in lock waits). With **two API processes** on an EPYC 7763 runner the suite passed: transfers p95
353 ms, accounts 83 ms, assistant 165 ms, login 520 ms, 0 errors, invariants intact.

The Dockerfile already runs one uvicorn process per container and says to "scale horizontally with replicas", but
no compose file ran more than one, and Prometheus scraped the static target `api:8000`, which is only correct for a
single process.

## Decision
- **Production runs two API replicas** (`infra/deploy/docker-compose.prod.yml`, `deploy.replicas:
  ${API_REPLICAS:-2}`). nginx already re-resolves `api` every 10 s and round-robins over all its addresses.
- **The local stack keeps one replica by default** (`${API_REPLICAS:-1}`), so the demo laptop and every CI job that
  uses `docker-compose.yml` behave as before. Its published port becomes the range `127.0.0.1:8000-8003`: the first
  replica still gets 8000, and `API_REPLICAS=2` no longer clashes.
- **The `Performance` workflow measures the production topology** (`API_REPLICAS=2`, overridable with the
  `api_replicas` input to measure one process). No threshold changes.
- **Prometheus discovers every replica** (`dns_sd_configs` on `api`, job `pbip-api`, one `instance` per replica), so
  `sum(rate(...))` stays exact. The worker totals (`source="worker"`, ADR-0039) and the PSI gauge (ADR-0038) are
  read from Redis and every replica serves the same values: `pbip-api` drops them and a second job,
  `pbip-api-shared` (static `api:8000`, any one replica), keeps only them, so they are counted once.
- The workflow also records what this investigation needed: per-container CPU during the run and the top
  statements from `pg_stat_statements` (load overlay only).

## Consequences
- Capacity per replica is about one core. Connections: each replica's pool is 10 + 10 overflow, so two replicas,
  the worker and beat stay well under PostgreSQL's `max_connections` of 100.
- Every replica loads the fraud models (about 250 MB resident per replica, measured with `docker stats`).
- Nothing in the API keeps security state in process memory: rate limits and velocity counters are in Redis;
  lockouts, refresh-token families and TOTP replay protection are in PostgreSQL. The readiness cache is per process
  and only shortens a health check.
- `/metrics` on the host port (8000) shows one replica's counters; Prometheus is the aggregate.

## Alternatives considered
- **Raise the thresholds or the runner size:** rejected; the budgets are the NFRs, and a larger runner would hide
  that one process saturates at this load.
- **Scale the API only in the load-test overlay:** rejected; the suite would measure a topology nothing deploys.
- **`uvicorn --workers N` in one container:** needs `prometheus_client` multiprocess mode, a shared metrics
  directory and a rework of the worker-metrics merge (ADR-0039). Replicas need none of that.
- **Make each request cheaper:** worth doing (the hybrid scorer's Isolation Forest costs ~15 ms per transfer on the
  reference laptop and is addressed separately), but no single change gives the 2x headroom the slower runners need.

## Standing-approval conditions
(1) No security, auth, MFA, RBAC, BOLA or audit change. (2) No money-path change; the ledger, lock order and
idempotency are untouched, and the load suite's invariants (no overdraft, balanced ledger, no deadlock) hold with
two replicas. (3) No migration. (4) No new external service or dependency: replicas of the existing image and
Prometheus's built-in DNS discovery. (5) Every number above comes from CI artifacts. (6) Within TASK-078/079/102.
