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
