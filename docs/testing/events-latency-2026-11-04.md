# Clickstream ingestion latency, 2026-11-04 (TASK-055, issue #78)

TASK-055's criterion 3 asks for "an asynchronous batch insert to minimise request latency (< 15 ms)". The
500 events/s load test had already passed on Windows (`infra/k6/events.js`, 4 of 4 runs), but nobody had measured
the latency of a single request or the cost of the insert. This page covers how the path works, what it costs,
the change that made the insert about three times cheaper, and where the 15 ms criterion stands. Every number
below comes from a run named here, on the machine stated for it.

## How the path works

`POST /api/v1/events/batch` (`modules/events/router.py` -> `service.py` -> `repository.py`) does all of its work
synchronously, inside the request:

1. It authenticates the request: it decodes the JWT, then runs one query for the token version and one for the
   customer row.
2. It applies the per-IP rate limit with a Redis `INCR`, plus an `EXPIRE` on the first call of a window.
3. Pydantic validates the body: 1 to 25 events (100 before ADR-0055, see the last section), with enums, regexes,
   bounded properties and a timestamp window.
4. It inserts the whole batch with **one** statement, `INSERT ... ON CONFLICT (id, event_timestamp) DO NOTHING
   RETURNING id`. PostgreSQL routes each row to its monthly partition.
5. It commits (a normal, synchronous commit), then returns 202 with `accepted` and `duplicates`.

**The insert is not asynchronous.** Nothing is queued, and the rows are committed before the 202 is sent. This is
deliberate, and the service docstring says so: a failure then surfaces as an error the client can retry, instead
of the batch being lost silently. Before this change, the single statement was built with
`insert(...).values(records)`. That is a multi-row `VALUES` statement with 8 × N bind parameters, and SQLAlchemy
built and compiled a new one on every call.

## The change

`repository.insert_events_batch` now runs one fixed statement:

```sql
INSERT INTO clickstream_events (id, event_timestamp, event_type, customer_id, session_id, page_url, properties, created_at)
SELECT * FROM unnest(:id, :event_timestamp, ...) ON CONFLICT (id, event_timestamp) DO NOTHING RETURNING id
```

It takes one array parameter per column. The SQL text is the same for every batch size, so SQLAlchemy compiles
it once and caches it. Everything else is unchanged: the API contract, validation, the transaction and commit, the
return value, partition routing and duplicate handling. No migration, no new dependency and no new background job.

The alternatives were measured in the same session (laptop, see the conditions below, 70 to 200 iterations each,
insert and commit together):

| Variant at 100 events | Median |
|---|---|
| `insert().values(records)` (before) | 121 ms |
| SQLAlchemy executemany ("insertmanyvalues", cached statement) | 27-38 ms |
| `INSERT ... SELECT FROM unnest(...)` (chosen) | 21-22 ms |
| `unnest` plus `SET LOCAL synchronous_commit = off` | 22 ms (no gain: the extra round trip cancels the commit saving) |

`COPY` was not used. It cannot do `ON CONFLICT`, so keeping resends idempotent would need a staging table and a
second statement. An asynchronous commit (`synchronous_commit = off`) did not help on this machine, so it was
dropped. It would also have given up durability for a gain we could not measure.

**Tests** (`tests/integration/test_events_repository.py`, real PostgreSQL, no timing asserts):

- Every column is stored as sent: all four property value types, a null customer and a null page.
- Each event lands in its monthly partition (checked with `tableoid`), this month and last month.
- A repeated event is skipped, both inside one batch and across batches: 100 events, 3 already seen, 97 inserted.
- An empty batch runs no statement.
- The regression test: batches of 1, 10 and 100 run the **same** single statement, once per batch. It fails on
  the old code. The four behaviour tests pass on both the old and the new code.

The existing 30 events tests are unchanged and pass: validation, authorization, rate limit, resend and partitions.

## Laptop measurements (before and after)

**Machine.** Intel Celeron N4500 (2 cores, 1.10 GHz), 7 GB RAM, PostgreSQL 16.15 in Docker (`synchronous_commit
= on`), shared with other agent sessions running mypy and pytest. The **1-minute load average was 8 to 13
throughout**, on 2 CPUs. The absolute numbers are therefore dominated by CPU queueing. The before and after runs
were interleaved, so the comparison between them is fair, but neither number is what an idle machine would show.

**Setup.** This was a scratch database (`pbip_bench_evt`, migrated to head and seeded with `scripts/seed_dev.py`),
not the demo database. Two uvicorn processes ran on the same database and Redis: `origin/main` (efadfc9) on :8078
and this branch on :8077. Both ran with `RATE_LIMIT_EVENTS_PER_MINUTE=100000`, so the limiter counted but never
blocked, and with `LOG_LEVEL=WARNING` and `--no-access-log`. The traffic was plain HTTP on loopback, from one
client, sequential, as `bob@demo.pbip`, who logged in once. There were 300 requests per batch size after 20
warm-up requests. Window: 04 Nov, 09:27-09:38 IST.

### (a) HTTP request latency, `POST /api/v1/events/batch` (client wall clock, ms)

| Batch | Round 1 before p50 / p95 / p99 | Round 1 after p50 / p95 / p99 | Round 2 before p50 / p95 / p99 | Round 2 after p50 / p95 / p99 |
|---|---|---|---|---|
| 1 | 74.7 / 124.4 / 155.0 | 95.8 / 168.2 / 216.9 | 82.5 / 129.0 / 169.0 | 32.9 / 58.8 / 90.6 |
| 10 | 107.0 / 162.9 / 204.9 | 97.4 / 191.4 / 240.1 | 82.7 / 148.5 / 213.9 | 34.3 / 52.0 / 71.9 |
| 100 | 314.2 / 543.1 / 979.2 | 150.4 / 226.2 / 269.8 | 223.7 / 438.3 / 568.5 | 81.4 / 146.9 / 198.1 |

Load average (1 min) per run, from start to end: round 1 before 10.1 -> 12.6, round 1 after 12.5 -> 13.3,
round 2 before 13.0 -> 9.2, round 2 after 9.3 -> 8.4. Every request returned 202. An earlier run of the old code,
started at a lower load (2.1, rising to 10.4; 500 requests), gave 27.2 / 55.4 / 106.6 ms for 1 event, 39.6 / 85.1 / 134.8 for 10
and 201.4 / 367.0 / 563.7 for 100.

At 100 events the new code is consistently faster: p50 is 2.1 to 2.7 times lower in both rounds. At 1 and 10
events the difference is within the noise of a machine with 4 to 6 runnable processes per core. There, the fixed
costs dominate: auth, two queries, Redis and the commit, plus waiting for a CPU.

### (b) Insert cost at the database (`measure_events_latency.py db`, ms per batch)

This is the repository call alone, then the commit, through the API's own engine, with 300 batches per size after
20 warm-up batches.

| Batch | Insert p50 / p95 / p99, before | Insert p50 / p95 / p99, after | Commit p50 (both) | Insert per event (p50), before -> after |
|---|---|---|---|---|
| 1 | 10.93 / 21.53 / 26.15 | 6.77 / 16.19 / 25.30 | 2.5 ms | 10.9 ms -> 6.8 ms |
| 10 | 23.57 / 46.57 / 60.83 | 11.04 / 20.99 / 30.79 | 2.7-2.8 ms | 2.36 ms -> 1.10 ms |
| 100 | 142.72 / 275.55 / 367.35 | 42.93 / 75.87 / 91.38 | 4.3 ms | 1.43 ms -> 0.43 ms |

Load average: before 8.4 -> 9.2, after 9.2 -> 9.4.

For comparison, PostgreSQL's own execution time for a 100-row insert, from `EXPLAIN ANALYZE` of an
`INSERT ... SELECT` from `generate_series` in the same database, was 8.2-8.7 ms (3 runs). That is about 80 µs per
row, for the primary key, three secondary indexes and the FK trigger. The rest of the 43 ms is Python-side
parameter processing plus CPU queueing on this machine.

## CI measurements (idle runner)

The `Performance` workflow now runs, after the k6 suite on the same stack:

- `infra/k6/events.js`: 500 events/s for 1 minute. Its thresholds gate the job: p95 < 500 ms, < 0.5% errors and
  no dropped batches.
- `measure_events_latency.py http`: through nginx TLS, as `load0001@perf.pbip`, 300 requests per size.
- `measure_events_latency.py db`: against the stack's database.

The two latency steps only record numbers. They are measurements, not gates, so a slow runner cannot turn the
job red on timing alone. Their output is in the `perf-results` artifact (`events-latency.*`, `events-insert.*`,
`k6-events-summary.json`).

Four `workflow_dispatch` runs, 04 Nov, on GitHub-hosted runners (4 vCPU, 16 GB). Each run used the full compose
stack with two API replicas behind nginx TLS and the load-test rate limits. The steps ran right after the main k6
suite, and the runner's 1-minute load average was 0.7 to 1.2 during the measurement steps (recorded by the
script). The "before" runs used `diag/events-latency-baseline` (b6daf54; the branch is deleted, the commit is kept as tag `archive/events-latency-baseline`). That branch is `origin/main` (efadfc9)
plus only this PR's workflow steps and script, so it measures the old insert. The "after" runs used this branch
(f309fc8). The runner CPU is assigned at random and differs between runs. The CPU column matters: the
EPYC 9V45 is clearly faster than the other two (see `performance-2026-11-04.md`).

**(a) HTTP latency through nginx TLS, sequential, 300 requests per size (ms, p50 / p95 / p99)**

| Run | Code | CPU | 1 event | 10 events | 100 events |
|---|---|---|---|---|---|
| 41781571714 | before | EPYC 9V45 | 4.74 / 6.36 / 7.02 | 6.04 / 6.96 / 7.71 | 19.34 / 23.48 / 95.12 |
| 41782452749 | before | Xeon 8573C | 6.31 / 6.84 / 7.05 | 8.33 / 9.07 / 10.96 | 25.16 / 28.43 / 36.81 |
| 41780626186 | after | EPYC 7763 | 6.97 / 8.15 / 8.85 | 7.90 / 9.05 / 10.59 | **15.12 / 19.77 / 22.84** |
| 41782456558 | after | EPYC 7763 | 6.84 / 8.07 / 9.36 | 7.24 / 7.97 / 8.35 | **16.63 / 18.93 / 22.62** |

**(b) Insert cost at the database, 300 batches per size (ms; per event = insert p50 / batch size)**

| Run | Code | CPU | 1 event: insert p50 / p95, commit p50 | 10 events: insert p50 / p95 | 100 events: insert p50 / p95, commit p50 | Per event at 100 |
|---|---|---|---|---|---|---|
| 41781571714 | before | EPYC 9V45 | 1.00 / 1.07, 0.27 | 2.18 / 2.34 | 13.23 / 15.12, 0.39 | 132 µs |
| 41782452749 | before | Xeon 8573C | 1.59 / 1.78, 0.45 | 3.30 / 3.48 | 18.83 / 19.40, 0.60 | 188 µs |
| 41780626186 | after | EPYC 7763 | 1.22 / 1.39, 0.39 | 1.91 / 2.10 | **7.84 / 8.32**, 0.55 | **78 µs** |
| 41782456558 | after | EPYC 7763 | 1.29 / 1.60, 0.40 | 1.99 / 2.21 | **8.23 / 10.46**, 0.59 | **82 µs** |

**k6, `infra/k6/events.js`, 500 events/s.** The run lasted 2 minutes (it inherits the workflow's `DURATION`):
601 batches of 100 events and 60,100 events stored. Every run had 0 failed requests and 0 dropped batches.

| Run | Code | CPU | Batch p50 / p95 / p99 |
|---|---|---|---|
| 41781571714 | before | EPYC 9V45 | 21.6 / 25.8 / 175.8 ms |
| 41782452749 | before | Xeon 8573C | 26.7 / 31.4 / 121.8 ms |
| 41780626186 | after | EPYC 7763 | 16.0 / 19.3 / 24.2 ms |
| 41782456558 | after | EPYC 7763 | 16.3 / 20.0 / 25.5 ms |

The insert for 100 events went from 13-19 ms to 8 ms, even though the "after" runs had the slower CPU. Per event,
that is 132-188 µs before and 78-82 µs after. The new figure is the same as PostgreSQL's own execution time
measured on the laptop, so almost nothing is left on the Python side. For 1 and 10 events, the old and new code are
within the CPU difference: there was little statement-building cost to remove.

## Where the 15 ms criterion stood after #449

Superseded by the batch cap in the next section; kept as the record of what the owner decided on.

- **Batches of 1 and 10 events: met** on an idle runner, through nginx TLS. The p95 was 6.4-9.1 ms and the p99
  7.0-11.0 ms, before and after this change. The web tracker sends small batches, so these sizes are its normal
  case.
- **Batches of 100 events (the maximum): not met.** After the change, p50 was 15.1-16.6 ms and p95 18.9-19.8 ms,
  down from 19.3-25.2 and 23.5-28.4 ms. The insert and commit now take about 9 ms of that. The rest is receiving,
  parsing and validating a body of about 20 KB, plus auth, the rate limit and TLS through nginx.
- **"Asynchronous": not done, deliberately.** The insert is still synchronous and committed before the 202, as
  `service.py` documents. Moving it off the request path would mean one of three things:
  - a Celery task per batch: a new background job, and `accepted`/`duplicates` could no longer be reported, which
    changes the contract;
  - an in-process queue, which loses events on a restart;
  - `synchronous_commit = off`, measured above with no gain.

  Each of these trades durability or the response contract for a few milliseconds that only the 100-event batch
  needs. That is an owner decision for an ADR (AGENTS.md rule 5), so it is not made in this PR.
- **Laptop numbers are not evidence for the 15 ms criterion.** They were taken at a 1-minute load average of 8-13
  on 2 CPUs. They show the relative improvement only.

## The batch cap (ADR-0055)

The owner approved an asynchronous insert only if no event can be lost, and otherwise a cap on the batch size
that keeps p95 under 15 ms. [ADR-0055](../adr/0055-clickstream-batch-cap-instead-of-asynchronous-insert.md) has
the analysis. In short: a PostgreSQL outbox row is the same durable insert and saves nothing; a Redis stream with
`appendfsync always` was fast enough but would make PBIP's Redis, which is not backed up and is documented as safe
to lose, the only durable copy of acknowledged events, and it cannot report `duplicates`. So the batch is capped at
**25 events**, the web tracker's own batch size; 26 or more gets a 422.

### Choosing the cap: two diagnostic runs (branch `diag/events-async-floor`, never merged; deleted, the commit is kept as tag `archive/events-async-floor`, 43a7de2)

That branch added two endpoints with the same auth, rate limit and validation as the real one: one that stores
nothing (the floor of any design) and one that does a single Redis `XADD` of the batch after `CONFIG SET
appendfsync always`. `measure_events_latency.py http` ran against all three through nginx TLS, sequential, 300
requests per size after 20 warm-up requests; the direct insert ran twice per run (before and after the
diagnostic endpoints). Both runs were on GitHub-hosted 4-vCPU runners; the runner's 1-minute load average was
0.9-1.5 during the steps.

**p50 / p95 / p99 (ms), run 41840655330, AMD EPYC 7763**

| Batch | Direct insert, pass 1 | Direct insert, pass 2 | Validate only | Redis XADD (fsync always) |
|---|---|---|---|---|
| 1 | 8.28 / 9.27 / 15.37 | 8.67 / 11.32 / 16.62 | 6.55 / 7.35 / 8.07 | 7.48 / 8.42 / 10.85 |
| 10 | 9.34 / 11.46 / 13.64 | 9.46 / 10.87 / 14.49 | 6.59 / 7.85 / 9.50 | 8.25 / 10.81 / 13.75 |
| 25 | 10.26 / **12.73** / 14.81 | 10.65 / **13.39** / 15.20 | 6.85 / 7.84 / 8.30 | 8.40 / 9.55 / 14.31 |
| 40 | 11.96 / 13.78 / 15.80 | 12.50 / **15.61** / 18.31 | 7.63 / 9.60 / 10.46 | 8.58 / 10.59 / 12.66 |
| 50 | 12.80 / **15.06** / 18.53 | 13.31 / **16.05** / 18.45 | 7.69 / 8.55 / 10.46 | 9.06 / 10.19 / 13.02 |
| 75 | 15.31 / 17.29 / 22.90 | 15.85 / 17.86 / 21.23 | 7.93 / 9.08 / 10.26 | 9.90 / 12.09 / 14.14 |
| 100 | 17.59 / 21.58 / 23.92 | 18.03 / 22.90 / 29.30 | 8.39 / 9.32 / 11.22 | 10.49 / 13.86 / 16.00 |

**p50 / p95 / p99 (ms), run 41842521457, Intel Xeon Platinum 8573C**

| Batch | Direct insert, pass 1 | Direct insert, pass 2 | Validate only | Redis XADD (fsync always) |
|---|---|---|---|---|
| 1 | 6.33 / 7.07 / 7.64 | 7.15 / 8.12 / 8.71 | 5.20 / 5.94 / 7.06 | 6.47 / 7.14 / 7.60 |
| 10 | 7.08 / 8.09 / 10.13 | 7.72 / 9.31 / 17.37 | 5.21 / 5.92 / 6.25 | 6.75 / 8.01 / 10.24 |
| 25 | 8.29 / **9.40** / 10.02 | 8.90 / **10.23** / 13.38 | 5.46 / 6.35 / 7.93 | 7.08 / 8.04 / 9.77 |
| 40 | 9.70 / 12.09 / 13.35 | 10.06 / 12.69 / 14.54 | 5.84 / 6.69 / 7.47 | 7.30 / 8.31 / 9.88 |
| 50 | 10.77 / 13.21 / 15.06 | 11.33 / 13.35 / 14.59 | 5.85 / 6.79 / 8.12 | 7.79 / 8.62 / 9.13 |
| 75 | 12.60 / 13.82 / 19.59 | 13.07 / **15.20** / 18.25 | 6.30 / 7.46 / 9.61 | 8.45 / 10.55 / 12.32 |
| 100 | 15.15 / 17.99 / 20.16 | 15.14 / 18.55 / 21.61 | 6.97 / 8.49 / 9.23 | 8.62 / 9.61 / 10.98 |

Insert plus commit at the database (`measure_events_latency.py db`, p50, 7763 / 8573C): 3.58 / 3.17 ms at 25
events, 4.86 / 3.92 at 40, 5.49 / 4.53 at 50, 7.41 / 6.00 at 75 and 9.32 / 7.40 at 100.

Readings:

- **25 is the largest measured size under 15 ms in all four direct passes** (p95 9.40-13.39 ms). 40 crossed it
  once (15.61 ms) and 50 twice (15.06 and 16.05 ms), both on the slower EPYC 7763.
- The validate-only floor at 100 events is 8.5-9.3 ms: about half the 100-event request is not the write at all.
- The durable Redis enqueue met 15 ms at every size (13.86 ms at 100 on the 7763). It was rejected for durability
  scope and the response contract, not for latency (ADR-0055, option 2).
- Single requests show rare outliers of 55-155 ms (the max column of the raw output) at most sizes and in every
  variant, the direct insert included; they are below the p99 and come from the runner, not the write path.

### After the cap: run 41844113321 on this branch (AMD EPYC 7763)

| Measurement | p50 | p95 | p99 |
|---|---|---|---|
| HTTP, 1 event (300 requests, all 202) | 7.66 | 9.08 | 10.87 |
| HTTP, 10 events | 8.04 | 9.42 | 10.41 |
| HTTP, 25 events (the cap) | 9.88 | **11.57** | 13.21 |
| Database insert plus commit, 25 events | 3.57 | 3.98 | 4.96 |
| k6 `events.js`, 500 events/s in batches of 25, 2 minutes | 9.31 | **11.44** | 13.72 |

Load average 1.41-1.48 during the HTTP step. The k6 run sent 2,401 batches of 25 (60,025 events, all stored), with
0 failed requests, 0 dropped batches and every check passing. Its summary reports 471 events/s because the rate
is averaged over the whole test, including the setup that logs in 100 users; the scheduled rate was 20 batches of
25 per second. Under that concurrent load a batch of 25 had a p95 of 11.4 ms, against 18.7-20.6 ms for batches of
100 in the diagnostic runs.

**Criterion 3 now:** p95 under 15 ms at every batch size the API accepts, on an idle CI runner through nginx TLS.
The insert is still synchronous and committed before the 202; "asynchronous" is not met, by the owner's decision.

## Commands

```bash
# scratch database (never the demo database), migrated and seeded
docker exec pbip-db-1 psql -U pbip -d postgres -qc "CREATE DATABASE pbip_bench_evt"
export APP_ENV=local DATABASE_URL=".../pbip_bench_evt?sslmode=require" REDIS_URL=redis://127.0.0.1:6379/9 \
       RATE_LIMIT_EVENTS_PER_MINUTE=100000 LOG_LEVEL=WARNING FIELD_ENCRYPTION_KEY=<generated>
(cd apps/api && alembic upgrade head) && python scripts/seed_dev.py
# before (origin/main worktree) on :8078, after (this branch) on :8077
uvicorn pbip_api.main:app --host 127.0.0.1 --port 8078 --no-access-log   # PYTHONPATH=<main worktree>/apps/api/src
uvicorn pbip_api.main:app --host 127.0.0.1 --port 8077 --no-access-log
# (a) HTTP, interleaved twice
python scripts/measure_events_latency.py http --base-url http://127.0.0.1:8078 --email bob@demo.pbip --requests 300
python scripts/measure_events_latency.py http --base-url http://127.0.0.1:8077 --email bob@demo.pbip --requests 300
# (b) database
PYTHONPATH=<main worktree>/apps/api/src python scripts/measure_events_latency.py db --email bob@demo.pbip --batches 300
python scripts/measure_events_latency.py db --email bob@demo.pbip --batches 300
# against the running stack (default events limit 120/min per IP: pace the requests)
python scripts/measure_events_latency.py http --base-url https://localhost:8443 --ca infra/certs/ca.crt \
       --email bob@demo.pbip --requests 100 --pace 0.5
# CI (idle runner): the Performance workflow runs all three on demand (HTTP sizes 1, 10 and 25 since ADR-0055;
# the db mode still accepts up to 100, e.g. --sizes 25 40 50 75 100)
gh workflow run perf.yml --ref perf/events-batch-latency
gh run download <run id> -n perf-results   # events-latency.txt, events-insert.txt, k6-events-summary.json
```
