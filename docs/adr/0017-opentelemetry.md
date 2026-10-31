# ADR 0017: OpenTelemetry distributed tracing (spike)
- Status: Accepted (spike: tracing is off by default; no collector is deployed)
- Date: 2026-10-31
- Deciders: Architecture Team (TASK-085, Platform & DevOps)

**Numbering:** the backlog names this file `0009-opentelemetry.md`, but 0009 is the synthetic
dataset schema ADR and numbers must be unique (`test_adr_index.py`), so it is 0017.

## Context
Logs carry a request id and Prometheus has route-level latency, but nothing shows where time goes
inside a request (policy checks, ledger locks, SQL). TASK-085 asks for a spike: trace FastAPI and
database calls, put `trace_id`/`span_id` in the structlog context, measure the overhead (< 5 ms per
request) and document the architecture.

## Decision
1. **Off by default** (`OTEL_ENABLED=false`). Settings (`core/config.py`):
   `OTEL_SERVICE_NAME` (default `pbip-api`), `OTEL_SAMPLE_RATE` (0-1, head sampling:
   `ALWAYS_ON`, `ALWAYS_OFF` or `TraceIdRatioBased`), `OTEL_EXPORTER`:
   * `none` (default): spans are created, so trace ids reach the logs, but nothing is exported;
   * `console`: spans printed to stdout through a `BatchSpanProcessor` (local development);
   * `memory`: every span kept in memory, for tests and the overhead evaluation only. Staging and
     production refuse to start with it (unbounded memory).
   There is **no network exporter**, so enabling tracing never adds a network dependency or a
   failure mode when a collector is down; the OTLP collector is roadmap item 1.
2. **One provider per process** (`core/tracing.py`): `setup_tracing` registers a `TracerProvider`
   with resource `service.name`, `service.version` (the package version) and
   `deployment.environment`, once; later calls return the same provider (the OpenTelemetry API
   refuses to replace the global provider). `shutdown_on_exit` flushes it at interpreter exit.
3. **FastAPI** (`instrument_app`, called by `create_app`): each request is a SERVER span named
   by method and route template; incoming W3C `traceparent` is honoured. `/api/health/*` and
   `/metrics` are excluded by path-anchored patterns (compose healthchecks and Prometheus scrapes
   would flood traces); any other path containing those strings is still traced.
   The ASGI `send`/`receive` sub-spans are disabled.
4. **SQLAlchemy** (`instrument_engine`, called by `core/db.get_engine` when enabled): each statement
   on the shared engine is a CLIENT child span (`db.system=postgresql`, statement with bind
   placeholders, never values). **sqlcommenter is off**: it would append a unique `traceparent`
   comment to every statement, which defeats psycopg's prepared-statement reuse and changes every
   query the database logs.
   * The instrumentation declares support for SQLAlchemy < 2.1 (still true of the latest release,
     0.66b2, checked on PyPI 2026-10-31); PBIP runs 2.1.x, so it is installed with
     `skip_dep_check=True`. The engine events it uses work on 2.1 (proved by
     `tests/integration/test_tracing.py` against PostgreSQL). `greenlet` is a direct dependency
     because SQLAlchemy 2.1 made it optional and the instrumented connect path needs it (without
     it those tests fail with `ModuleNotFoundError: greenlet`).
5. **Log correlation** (`core/logging.py`): `_opentelemetry_processor` adds `trace_id` (32 hex) and
   `span_id` (16 hex) to every log line written inside a span, before the redaction processor.
   With tracing off the API returns an invalid no-op span and nothing is added.

## Data protection
Spans follow the access-log policy (`core/middleware.py`): no query strings (a server request hook
strips them from `http.url`/`http.target`), no headers, no bodies, no SQL values. Spans do contain
the client IP and user agent recorded by the ASGI instrumentation, and URL paths, whose only
variables are UUIDs. With `console`, spans go to stdout next to the logs and bypass the log
redaction processor, which is why `console` is a development setting. Threat model T33.

## Overhead evaluation
`scripts/measure_tracing_overhead.py` runs the real app in-process against PostgreSQL in three
fresh interpreters (off, `none`, `memory`) and sends sequential `GET /api/v1/accounts` requests
(authentication, policy check, SQL). The earlier timing assertion in the unit suite was removed:
wall-clock assertions are non-deterministic (AGENTS.md section 10) and it measured a toy app on SQLite.

Measured 2026-10-31 on a 2-CPU Linux laptop (Python 3.12.15, PostgreSQL 16 and Redis in Docker),
three runs of 500 requests after 50 warm-up requests each. Times in ms; deltas against tracing off.
Not a CI gate.

| Run | off median / p95 | `none` median / p95 | `memory` median / p95 | delta `none` median / p95 | delta `memory` median / p95 |
|---|---|---|---|---|---|
| 1 | 9.6 / 16.9 | 10.9 / 16.1 | 11.1 / 16.3 | +1.3 / -0.8 | +1.5 / -0.6 |
| 2 | 9.7 / 15.8 | 11.3 / 16.0 | 12.6 / 19.2 | +1.6 / +0.2 | +2.9 / +3.4 |
| 3 | 11.2 / 17.2 | 12.4 / 18.2 | 12.8 / 19.0 | +1.2 / +1.0 | +1.6 / +1.8 |

Tracing adds about 1.2-1.6 ms at the median with no exporter, and 1.5-2.9 ms when spans are also
exported in memory, so it stays under the 5 ms target. The p95 deltas are within run-to-run noise
on this machine (between -0.8 and +3.4 ms). These are local numbers: the exporter cost of OTLP over
the network is not measured, and load and staging measurements are tracked in #101 (TASK-078) and #198.

## Not covered by this spike
* **Celery workers** are not traced: `setup_tracing` runs only in `create_app`. A worker with
  `OTEL_ENABLED=true` instruments its engine against the no-op global provider, so it records
  nothing and changes nothing. Roadmap item 3.
* Redis, outbound HTTP and metrics through OpenTelemetry (Prometheus stays the metrics path; the
  FastAPI instrumentation's metrics go to the no-op global meter provider).

## Consequences
* Correlated logs and traces for local debugging; no production behaviour change while off.
* Dependencies: `opentelemetry-instrumentation-fastapi` and `-sqlalchemy` (0.66b1, beta releases
  of the contrib packages), their `-asgi`, `-util-http` and `wrapt` dependencies, and `greenlet`.
  `opentelemetry-api`/`-sdk` were already locked (MLflow) and are now direct dependencies.

## Roadmap
1. OTLP exporter to an OpenTelemetry Collector (or Tempo/Jaeger) via `OTEL_EXPORTER_OTLP_ENDPOINT`,
   with timeouts and a bounded queue so an unavailable collector drops spans instead of blocking.
2. Production sampling around 10% (`OTEL_SAMPLE_RATE=0.1`), tail sampling for errors in the collector.
3. Celery (`opentelemetry-instrumentation-celery`, with `traceparent` propagated in task headers).
4. Redis instrumentation for rate limits and idempotency lookups.
