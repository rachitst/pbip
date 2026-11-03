# ADR 0038: Scheduled PSI drift evaluation, exported through the API's /metrics
- Status: Accepted (under owner's standing approval, 2026-11-03)
- Date: 2026-11-03
- Deciders: Architecture Team (issue #324, follow-up to TASK-068 / #91)

## Context
TASK-068 (#336) shipped the PSI maths (`pbip_ml.monitoring.psi.drift_report`: amount and per-account daily
velocity, current `[end-30d, end)` vs reference `[end-60d, end-30d)`) and the `pbip_feature_psi{feature}` gauge
(`core/metrics.record_feature_psi`), but nothing computes PSI on a schedule, so the gauge is never set and the
acceptance criteria "flags warning > 0.1 / critical > 0.25" and "logs drift metrics to Prometheus / Grafana" are
not met. A new background job is an architectural change (AGENTS.md rule 5).

The constraint that shapes the design: **Prometheus scrapes only the API** (`pbip-api` job, `api:8000/metrics`)
plus exporters. The Celery worker is a different process with no metrics endpoint, and runs tasks in prefork
children, so a gauge set inside a task is invisible to Prometheus. (The worker-side counters that exist today,
`scheduled_transfers_total` and `fd_maturity_payouts_total`, have the same gap; see Consequences.)

## Decision
1. **Compute in the worker, hourly.** New beat entry `evaluate-feature-drift` -> task
   `pbip.fraud.evaluate_feature_drift`, `crontab(minute=20)` (away from the `:05` payout run). The task calls
   `fraud/drift.evaluate_feature_drift(session, redis, utcnow())`, which reads the last 60 days of scored outgoing
   payments through a new read-only ledger function, `ledger.service.scored_payments_between(session, start, end)`
   (amount, debit account id, created_at of `TRANSFER` and `BILL_PAYMENT` rows in any stored status: every row was
   scored, so all are the scorer's input distribution), and runs `drift_report(..., window_end=now)`. No other
   module's internals are touched; the PSI maths is the existing pure `pbip_ml.monitoring` package (numpy only, no
   training code), already imported by `core/metrics.py`.
2. **Hand the result to the API through Redis.** The job stores `{window_end, results: [{feature, psi,
   reference_size, current_size}]}` under `fraud:psi:v1` with a **3-hour TTL** (two missed runs). Only feature
   names and numbers are stored.
3. **Export from the API.** `/metrics` calls `fraud/drift.publish_feature_psi(redis)` before `generate_latest()`:
   one Redis GET, then `pbip_feature_psi` is cleared and set from the stored results. Stored values are
   re-validated (feature in the allow-list `amount`/`velocity`, finite, non-negative), so a corrupted key cannot add
   label values or publish garbage. A lock keeps a concurrent scrape from seeing the gauge mid-refresh. Redis
   unreachable at scrape time -> no PSI series, the rest of `/metrics` is served as before.
4. **No data is no series, never zero.** Fewer than `MIN_WINDOW_PAYMENTS` (100) payments in either window (fewer than
   10 per decile bin: PSI would measure sampling noise), an expired key, or an unreadable one: the job deletes the
   key / the API exports nothing. Zero would claim "stable" for a value nobody computed.
5. **Alerts and dashboard.** `FeatureDriftWarning`: `pbip_feature_psi >= 0.1 and pbip_feature_psi <= 0.25`,
   `for: 150m`; `FeatureDriftCritical`: `pbip_feature_psi > 0.25`, `for: 90m`. The bounds are
   `pbip_ml.monitoring.psi.classify`'s (0.1 is already a warning, 0.25 still a warning), so at most one fires per
   feature. The gauge changes once an hour, so `for:` counts hourly results: three in a row in the warning band
   (moderate drift is a slow trend; do not flap on one hour), two in a row above 0.25 (act within about two hours,
   never on a single run). Both are `severity: ticket`: drift over a 30-day window is a model-quality issue, not an
   outage. The fraud dashboard gets a "Model input drift" row with the PSI time series and both thresholds drawn.

## Alternatives considered
- **Expose the worker's own metrics** (`prometheus_client.start_http_server` + a new scrape target). Prefork
  children would each hold their own registry, so it needs `PROMETHEUS_MULTIPROC_DIR` multiprocess mode, a shared
  volume and a new scrape job in three compose files. More moving parts for one hourly number.
- **Compute on scrape in the API** (a custom collector that queries 60 days and runs PSI). SQL plus numpy every
  15 s in every API process, on the path Prometheus times out on. Rejected.
- **A periodic thread in the API process.** A second scheduler beside Celery beat, duplicated per uvicorn worker,
  with its own failure handling; AGENTS.md section 2 keeps background work in the worker.
- **Persist the result in PostgreSQL.** Needs a table and migration (the issue expects none) for a value that is
  recomputed hourly and has no history requirement (Prometheus keeps the history).

## Consequences
- Good: one hourly read-only query, no migration, no new dependency, service or scrape target; the gauge reaches
  Prometheus through the existing `pbip-api` job; job failures surface through the existing `CeleryTaskFailures`
  alert; the result disappears by itself if the job stops (TTL), so stale drift never keeps alerting.
- Bad: the PSI series depends on Redis being up at scrape time (Redis has its own exporter and alerts). The 60-day
  query filters on `transactions.created_at` without a dedicated index; at simulator scale this is a sub-second
  hourly scan, and an index is a later migration if volume grows. With several API replicas each exports the same
  value, so the dashboard uses `max by (feature)`.
- To do: the existing worker-side counters (`scheduled_transfers_total`, `scheduled_worker_duration_seconds`,
  `fd_maturity_payouts_total`) are never scraped for the reason above; this ADR does not change them. That gap
  needs its own decision and issue.

## Standing-approval check (all six hold)
1. No security weakening: no new endpoint; `/metrics` stays internal-only and public-by-design as before; no
   customer data, account ids or amounts in Redis, logs or labels.
2. No money movement: the job only reads `transactions`.
3. No schema change, so no migration.
4. No new external service, paid dependency or outbound data: Celery beat, Redis and Prometheus already run.
5. No fake data: values come from real stored payments; with too little data the series is absent, not zero.
6. In scope: TASK-068's acceptance criteria and issue #324.
