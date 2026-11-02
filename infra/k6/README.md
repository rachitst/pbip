# k6 performance baseline (TASK-012)

`baseline.js` drives the unauthenticated health endpoints `/api/health/live` and
`/api/health/ready` with **50 virtual users held for 30 s** (after a 5 s ramp-up) and fails (exit code 99) unless:

| Threshold | Limit |
|---|---|
| p95 latency, all requests and per endpoint | < 50 ms |
| HTTP error rate | < 0.1 % |
| Response checks passing | > 99.9 % |

## Run it

Start the API first (`python scripts/dev.py dev-api`, or the full stack with `make up`), then:

```bash
make perf                        # Linux/macOS
.\dev.ps1 perf                   # Windows PowerShell   (dev.cmd perf on CMD)
python scripts/dev.py perf       # any OS
```

The runner uses a native `k6` if it is on `PATH`, otherwise the pinned `grafana/k6` Docker image
(Linux: host networking; Docker Desktop on macOS/Windows: `host.docker.internal`). Options are
environment variables: `BASE_URL` (default `http://localhost:8000`; use `http://localhost:8080` to go
through nginx), `VUS`, `DURATION`, `THINK_SECONDS` (mean think time, randomised +/-50%).

## Recorded baseline (2026-10-25, 2-core Ubuntu laptop; k6, API and PostgreSQL on the same host)

| Endpoint | p50 | p95 | p99 | Errors |
|---|---|---|---|---|
| live | 1.8-1.9 ms | 5.4-5.6 ms | 9.3-9.9 ms | 0 % |
| ready (1 s result cache, default) | 5.4-5.5 ms | 24.5-26.4 ms | 38.4-40.8 ms | 0 % |
| ready (cache disabled, `HEALTH_READY_CACHE_SECONDS=0`) | 8.3 ms | 34.5 ms | 57.8 ms | 0 % |

Two runs with the default configuration, one with the cache disabled; ~178 requests/s, single uvicorn worker.

## Notes from building it

* With a fixed `sleep(0.5)`, all 50 VUs fire in the same millisecond every iteration; that
  synthetic thundering herd pushed `/ready` p95 to ~125 ms at the same request rate. Think time
  is randomised so the test measures steady load.
* A 5 s ramp-up avoids the same burst at start-up: in short runs the first, synchronised iteration
  landed near the 95th percentile on its own.
* `/live` is `async` (no I/O, no threadpool hop). `/ready` caches its DB+Redis result for 1 s
  (`HEALTH_READY_CACHE_SECONDS`): it is a public endpoint, and without the cache every request is
  a database round trip anyone can trigger.
* This is a baseline, not a stress test: authenticated journeys and saturation testing are the
  Sprint 6 k6 suite.

## Load suite (TASK-078): `load.js`

Authenticated banking traffic with the NFR budgets as thresholds. Per iteration (weighted random):
50% account reads (list, detail, a transactions page), 12% per-account analytics, 8% assistant questions
the deterministic router answers without the LLM, 20% transfers (fresh `Idempotency-Key`; 1 in 10 is
replayed and must return the same transfer marked `Idempotent-Replayed`), 10% login plus a cookie refresh.

```bash
docker compose -f docker-compose.yml -f infra/k6/compose.load.yml up -d --build --wait   # raised per-IP limits
uv run python scripts/seed_load.py --users 200            # funded users, each with a verified payee
uv run python scripts/load_invariants.py snapshot > perf-results/before.json
BASE_URL=https://localhost:8443 VUS=50 DURATION=3m USERS=200 make load   # or: python scripts/dev.py load
uv run python scripts/load_invariants.py check perf-results/before.json  # exit 1 on any violation
```

* `compose.load.yml` raises the per-IP login and assistant limits because every virtual user comes from one IP;
  never use it outside a load test. The limiters stay covered by their own tests.
* Thresholds: p95 < 500 ms per route group (accounts, analytics, assistant, transfers), login p95 < 3 s, refresh p95
  < 500 ms, HTTP errors < 0.5%, zero unexpected statuses. Correct business outcomes are not errors: 201 posted,
  202 held for step-up or review, 403 blocked by fraud scoring, and forecast's 422 for accounts with under two
  days of history.
* `load_invariants.py` proves the money rules held: no overdrawn account, every transaction balanced, each balance
  equal to its last entry, money conserved inside the load population, and no new PostgreSQL deadlocks.
* `.github/workflows/perf.yml` runs the suite and the page-load budgets (`apps/web/e2e/perf.spec.ts`) nightly on a
  GitHub-hosted runner and uploads `perf-results/` with the runner's hardware. Results and their limits:
  `docs/testing/performance-2026-11-01.md`.
