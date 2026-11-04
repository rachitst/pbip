# ADR 0055: Cap a clickstream batch at 25 events instead of inserting asynchronously
- Status: Accepted (owner decision on #78, 2026-11-04: "asynchronous only if events can't be lost; otherwise cap
  the batch size so p95 stays under 15 ms, and document it")
- Date: 2026-11-04
- Deciders: Architecture Team (TASK-055, issue #78)

## Context
TASK-055's criterion 3 asks for an "asynchronous batch insert to minimise request latency (< 15 ms)".
`POST /api/v1/events/batch` authenticates (JWT, token-version query, customer query), applies the per-IP rate
limit in Redis, validates the body with Pydantic, inserts the batch with one compiled-once
`INSERT ... SELECT FROM unnest(...)` (#449), commits, and returns 202 with `accepted` and `duplicates`.

After #449 the Performance workflow (idle 4-vCPU runner, nginx TLS, sequential requests) measured a p95 of
8.1-9.1 ms for 1-10 events but about 19 ms for 100 events, the old `MAX_BATCH_EVENTS`. The insert and commit are
about 9 ms of those 19; the rest is receiving, parsing and validating about 20 KB, auth and TLS. The web tracker,
the only client, sends at most 25 events per batch (`apps/web/src/telemetry/tracker.ts`).

The owner approved an asynchronous insert **only if no event can be lost**: an event must be durably stored before
the 202 is sent, with tests for a crash before the flush. Otherwise the batch is capped.

## Measurements
Two `workflow_dispatch` runs of the Performance workflow on `diag/events-async-floor` (never merged), which added
two diagnostic endpoints next to the real one, with the same auth, rate limit and validation:

- **validate only**: stores nothing. This is the floor of *any* design, asynchronous or not.
- **Redis XADD**: one `XADD` of the whole batch to a stream after `CONFIG SET appendfsync always`, so Redis fsyncs
  its AOF before it replies. This is the cheapest design that could honestly claim "durable before the 202".

Each cell is p95 in ms over 300 sequential requests after 20 warm-up requests, through nginx TLS. The direct insert
was measured twice per run (before and after the diagnostic endpoints), so each run gives two direct values.

| Batch | Direct insert, run 41840655330 (EPYC 7763) | Direct insert, run 41842521457 (Xeon 8573C) | Validate only (7763 / 8573C) | Redis XADD, fsync always (7763 / 8573C) |
|---|---|---|---|---|
| 1 | 9.27, 11.32 | 7.07, 8.12 | 7.35 / 5.94 | 8.42 / 7.14 |
| 10 | 11.46, 10.87 | 8.09, 9.31 | 7.85 / 5.92 | 10.81 / 8.01 |
| **25** | **12.73, 13.39** | **9.40, 10.23** | 7.84 / 6.35 | 9.55 / 8.04 |
| 40 | 13.78, **15.61** | 12.09, 12.69 | 9.60 / 6.69 | 10.59 / 8.31 |
| 50 | **15.06, 16.05** | 13.21, 13.35 | 8.55 / 6.79 | 10.19 / 8.62 |
| 75 | **17.29, 17.86** | 13.82, **15.20** | 9.08 / 7.46 | 12.09 / 10.55 |
| 100 | **21.58, 22.90** | **17.99, 18.55** | 9.32 / 8.49 | 13.86 / 9.61 |

Runner 1-minute load average 0.9-1.5 during the steps. Insert plus commit at the database (`measure_events_latency.py
db`), p50: 3.2-3.6 ms at 25 events, 4.5-5.5 ms at 50, 7.4-9.3 ms at 100.

## Options considered

1. **An outbox row in PostgreSQL, relayed later (ADR-0040's pattern).** Durable, but it is an insert of the same
   rows in the same database and the same commit before the 202, so it saves nothing; it would add a relay job and a
   second copy of every event. Rejected.
2. **A Redis stream with `appendfsync always`, drained by a Celery beat job.** It meets 15 ms at 100 events in both
   runs (13.86 and 9.61 ms). It is rejected because it does not meet the owner's condition as this system is built
   today:
   - PBIP's Redis is not durable storage. The runbook says it "holds only rate-limit counters and the Celery queue,
     which are safe to lose"; it is not in `scripts/backup.py`'s scope, and the production compose file runs it
     with the default `appendfsync everysec`, which can lose about 1 s of acknowledged writes on a crash. An
     acknowledged event would sit, for up to one relay interval, in a store that the backups and restore drills do
     not cover.
   - Making it durable means `appendfsync always` on the shared instance, which puts an fsync on every rate-limit
     `INCR`, idempotency fast-path and Celery message on every money path. That cost was not measured and is not
     justified by analytics. The alternative, a second Redis just for events, is a new service with its own volume,
     backup and monitoring (AGENTS.md section 2: no new service without an ADR of its own).
   - The 202 could no longer report `duplicates`: the de-duplication (`ON CONFLICT (id, event_timestamp)`) happens
     at the flush. `accepted` would mean "queued". The web tracker ignores the counts, but k6 and the API contract do
     not; changing their meaning is a v1 contract change.
   - It adds a beat job, consumer-group bookkeeping (pending entries, claim after a crash, poison batches,
     dead letters) and lag monitoring, for a gain only a 26-100-event batch needs. No client sends one.
3. **An in-process queue or a Celery task per batch.** An in-process queue loses everything on a restart; a Celery
   message goes through the same Redis (option 2's durability problem) and loses the counts. Rejected.
4. **`synchronous_commit = off`.** Measured in #449: no gain (the extra round trip cancels the saving), and it gives
   up durability. Rejected.
5. **Cap the batch.** Chosen.

## Decision
- `MAX_BATCH_EVENTS` goes from 100 to **25** (`modules/events/schemas.py`). A larger batch gets the standard
  RFC 7807 422 (`errors[0].loc = ["body", "events"]`, `type = "too_long"`, "List should have at most 25 items after
  validation, not N"). Nothing from a refused batch is stored.
- **Why 25:** it is the largest size measured that stayed under 15 ms in all four direct measurements (p95
  9.40-13.39 ms). 40 reached 15.61 ms and 50 reached 16.05 ms on the slower runner CPU. It is also exactly what the
  web tracker sends (`MAX_BATCH_SIZE = 25`), so the app's batches do not change. A unit test keeps the tracker's
  constant at or below the API's.
- The insert stays synchronous and committed before the 202. `accepted`/`duplicates` keep their meaning.
- `infra/k6/events.js` now defaults to batches of 25 (20 batches/s for 500 events/s; its limit is 1-25), and
  `scripts/measure_events_latency.py http` measures 1, 10 and 25 events.

## Consequences
- **API (v1, stricter validation):** a client sending 26-100 events per batch now gets a 422 and must split its
  batches. The only client (the web app) sends 25, so no customer sees a change. This is a tightening of v1
  validation, recorded in the OpenAPI contract (`maxItems: 25`).
- **Throughput:** 500 events/s now needs 20 requests/s instead of 5. Performance run 41844113321 on this change
  sustained it for 2 minutes with 0 failed requests and 0 dropped batches, at a batch p95 of 11.4 ms (18.7-20.6 ms
  for batches of 100 before); the sequential p95 at 25 events was 11.57 ms
  (`docs/testing/events-latency-2026-11-04.md`). The per-IP events limit
  (120/min by default) is per browser, which sends at most 10 batches a minute, so it is unchanged.
- **Security:** a smaller body per request; no new attack surface, so no new threat-model row (T49 covers the
  endpoint and is unchanged).
- **The criterion is met by a cap, not by asynchrony.** TASK-055's "asynchronous" wording is not met. The
  < 15 ms target is met at every allowed size on an idle CI runner.

## Revisit when
A client needs batches above 25 at < 15 ms, or Redis (or another queue) becomes durable, backed-up storage under
its own ADR. Then option 2 is the measured candidate: its p95 at 100 events was 9.6-13.9 ms, and it would need a
contract change for the counts and crash-before-flush tests that prove every acknowledged event is stored exactly
once.
