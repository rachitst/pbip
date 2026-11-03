# ADR 0039: Worker metrics reach Prometheus through Redis and the API's /metrics
- Status: Accepted (under owner's standing approval, 2026-11-03)
- Date: 2026-11-03
- Deciders: Architecture Team (issue #378, follow-up to ADR-0038)

## Context
Prometheus scrapes one application target, `api:8000/metrics` (job `pbip-api`), plus exporters. The Celery worker has
no metrics endpoint and runs tasks in prefork children, so anything a task adds to a `prometheus_client` metric stays
in that child's memory and is never scraped. That loses:
- the worker-only series `scheduled_transfers_total{outcome}`, `scheduled_missed_occurrences_total`,
  `scheduled_worker_duration_seconds` (beat `execute-scheduled-transfers`) and `fd_maturity_payouts_total{outcome}`
  (beat `pay-out-matured-deposits`);
- the shared series the jobs also update because they run the normal payment path: a scheduled transfer goes through
  `PaymentsService`, so `transfers_total`, `fraud_decisions_total`, `fraud_inference_seconds` and
  `idempotent_replays_total` miss every scheduled payment. The step-up expiry job of #369 adds
  `transfers_total{status="DECLINED"}` from the worker in the same way.

The jobs are audited and logged, so only the series are missing. ADR-0038 (PSI) chose "the worker writes to Redis,
the API's `/metrics` exports it" for one gauge. This ADR chooses how all worker counters and histograms are exported.

## Decision
Option 2 of the issue, the same pattern as ADR-0038, made generic so no call site changes
(`core/worker_metrics.py`):
1. **The worker publishes deltas after every task.** Celery's `task_postrun` signal (connected in `worker/tasks.py`)
   calls `WorkerMetricsPublisher.flush`: for every `Counter` and `Histogram` in `core/metrics.py` it computes what
   changed since this process's last successful flush and adds it to one Redis hash, `pbip:metrics:worker:v1`, with
   `HINCRBYFLOAT` in one `MULTI/EXEC` (atomic, and prefork children add up). A histogram's buckets, `_count` and `_sum`
   for a label set are always written together, so a stored histogram is always complete. `_created` samples are not
   published (they are per-process timestamps).
2. **Only worker processes publish.** `worker_init` and `worker_process_init` mark the process; `task_postrun` in any
   other process (an eager `apply()` in the API or a test) does nothing. The API's own increments stay in-process and
   the worker's go only through Redis, so nothing is counted twice, even on code paths both run (`PaymentsService`).
3. **The API exports them with `source="worker"`.** `/metrics` reads the hash once (`HGETALL`) and appends each
   stored series to the same family as an extra series labelled `source="worker"`. Existing queries such as
   `sum by (status) (rate(transfers_total[5m]))` include worker activity with no dashboard change, and
   `{source="worker"}` isolates it. The API-side series keep their labels, so no existing series changes identity.
4. **Counters stay monotonic.** The hash has no TTL and is only incremented; Redis runs with AOF (`--appendonly yes`)
   in both compose files. If the data is lost (flush, volume reset) the totals restart at zero, which Prometheus
   treats as an ordinary counter reset: `rate()`/`increase()` stay correct. Nobody deletes the key to "clear" a
   counter (runbook).
5. **Fail open, never fail the job or the scrape.** A Redis error at flush is logged (`worker_metrics_flush_failed`,
   error type and series count only) and the delta stays pending for the next task. A Redis error at scrape time
   drops only the worker series; the rest of `/metrics` is served as before.
6. **Fixed cardinality, untrusted input.** The values in Redis are re-validated on every scrape: the sample name must
   belong to a counter or histogram in `core/metrics.py`, the label names must be exactly that metric's (plus `le`
   on buckets), every label value must match `^[A-Za-z0-9_.+-]{1,64}$` (the call sites pass enum values; bucket bounds
   are numbers) and the value must be finite and non-negative. Invalid fields are skipped and counted in one warning;
   a hash with more than 1,000 fields (today's metrics need fewer than 200) is ignored entirely.
7. **Guard.** `test_every_application_metric_is_exported_from_the_worker_or_set_by_the_api` fails when a metric type
   the publisher cannot carry (a `Gauge`, a `Summary`) is added to `core/metrics.py` without being declared API-side;
   `test_every_counter_and_histogram_reaches_the_api_metrics` updates every counter and histogram as a worker would
   and fails if any does not appear in `/metrics` with `source="worker"`.

## Alternatives considered
- **Option 1: `prometheus_client` multiprocess mode in the worker** with an HTTP server on a sidecar port and a new
  scrape job. Needs `PROMETHEUS_MULTIPROC_DIR` on a shared volume, cleanup of dead children's files, a new port and
  scrape job in three compose files and a second exporter process. More moving parts than the problem, and it would
  diverge from ADR-0038.
- **Option 3: derive the series from PostgreSQL** (postgres-exporter custom queries over audit rows or transactions).
  Durations and fraud-scoring latency are not stored anywhere, so the histograms could not be rebuilt; it would also
  put SQL on the 15-second scrape path and duplicate business logic in exporter config.
- **Redis-backed counters at every call site** (a custom metric class that writes to Redis on each `inc()`). Changes
  every call site, puts a Redis round trip into the payment request path in the API, and needs a process switch
  anyway for code both processes run. The post-task delta flush gives the same result with no call-site change.
- **Merge worker totals into the API series without a label.** The sum of an API-process counter and the Redis total
  drops whenever the API restarts; Prometheus would read that as a reset and add the whole worker total again on every
  restart. A separate `source="worker"` series has its own reset semantics.

## Consequences
- Good: all application counters and histograms updated in the worker reach Prometheus through the existing
  `pbip-api` scrape job; scheduled payments now appear in the transfer and fraud panels and in `FraudBlockRateHigh`;
  the change is in `core/`, the `/metrics` handler and the task module: no module, migration, dependency, service or
  scrape target is added; #369's step-up expiry increments are carried with no change to its code.
- Bad: worker series lag by at most one task (the flush runs after each task; the outbox relay runs every 10 s), and an
  increment made by a child that is killed before its task ends is lost. Celery uses the same Redis as its broker, so
  while Redis is down tasks do not run either. With a connection dropped during `EXEC` a batch could in theory be
  applied and retried once (Redis reports no result); this can only over-count one task's increments. If several API
  replicas are ever scraped as separate targets, each exports the same worker totals, so queries over
  `source="worker"` must then use `max without (instance)`; today there is one target (`api:8000`).
- The publisher reads `prometheus_client`'s private `_labelnames` (no public accessor); the version is pinned in
  `uv.lock` and the guard tests break loudly if it changes.

## Standing-approval check (all six hold)
1. No security weakening: no new endpoint or route; `/metrics` stays internal-only and not proxied by nginx; Redis
   holds only metric names, enum label values and numbers, never ids, amounts or personal data; stored values are
   re-validated before export (threat row T58).
2. No money movement: metrics only; the ledger, lock order and idempotency are untouched.
3. No schema change, so no migration.
4. No new external service, paid dependency or outbound data: Redis, Celery and Prometheus already run.
5. No fake data: every exported value is a real increment made by a job; with Redis unavailable the series is absent,
   never invented or zero-filled.
6. In scope: issue #378 (observability NFR, TASK-069/103 monitoring as code).
