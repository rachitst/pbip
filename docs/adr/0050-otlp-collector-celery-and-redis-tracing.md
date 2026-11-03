# ADR 0050: OTLP export to an optional local collector, with Celery and Redis tracing
- Status: Accepted (under owner's standing approval, 2026-11-04)
- Date: 2026-11-04
- Deciders: Architecture Team (issue #209, items 1-3; follows ADR 0017)

## Context
ADR 0017 added tracing as a spike with no network exporter: spans could be printed (`console`) or
kept in memory (tests), but not sent anywhere. Issue #209 lists the follow-ups. The owner asked for
item 1 (an OTLP exporter and a collector) as an optional compose profile, and for items 2 and 3
(Celery and Redis) if they can be done safely. Item 3 had a stated risk: rate-limit keys embed the
client IP (`rl:{name}:{ip}`, `core/ratelimit.py`) and velocity keys embed account ids, so Redis
tracing is acceptable only if no key can reach a span. Item 4 (staging overhead) still needs a
staging host (#198) and is not part of this decision.

## Decision
1. **`OTEL_EXPORTER=otlp`** sends spans over **OTLP/HTTP** (protobuf) to
   `OTEL_EXPORTER_OTLP_ENDPOINT` + `/v1/traces`. The endpoint is fixed configuration validated to
   scheme, host and port (no credentials, path or query), the same rule as `OLLAMA_BASE_URL`.
   Default `http://127.0.0.1:4318` (the published port; `localhost` resolves to `::1` first on
   Windows). It is allowed in staging and production; `memory` is still refused there.
2. **A collector that is down or hanging only costs dropped spans.** The exporter sits behind a
   `BatchSpanProcessor` with a queue of at most 2,048 spans (overflow is dropped and logged by the
   SDK), batches of 512 every 5 s, on the processor's own thread. A request thread only appends to
   the queue. One export, retries included, gives up after `OTEL_EXPORT_TIMEOUT_SECONDS` (default
   2, range 0-10); the SDK's `export_timeout_millis` is not applied by this SDK release (its source
   says so), so the bound comes from the exporter's own timeout.
3. **CLIENT spans without a parent are not sampled** (`_DropOrphanClientSpans`, wrapping the
   existing ratio sampler). Readiness probes and the Prometheus scrape run SQL and Redis commands
   outside any traced request; each would otherwise be a one-span trace every few seconds.
4. **Celery** (`opentelemetry-instrumentation-celery`): `worker_process_init` (after the prefork
   fork, so the export thread lives in the child) and `beat_init` call `setup_celery_tracing`, which
   registers a provider named `<OTEL_SERVICE_NAME>-worker` or `-beat` and connects the Celery
   signals. Beat's publish injects W3C `traceparent` into the task headers and the worker continues
   it, so `apply_async/<task>` and `run/<task>` share one trace. Spans record the task name, id and
   routing, never task arguments.
5. **Redis** (`opentelemetry-instrumentation-redis`): only the application's client (`get_redis`) is
   instrumented, with `instrument_client`; a global `instrument()` would also trace Celery's broker
   polling in every worker. The instrumentation already records `?` for every argument
   (`INCRBY ? ?`); our request and response hooks reduce the statement to the command names
   (`INCRBY`, or `ZADD\nZREMRANGEBYSCORE\nEXPIRE` for a pipeline), so a key cannot reach a span even
   if a later release starts recording arguments.
6. **Compose profile `otel`**: `otel/opentelemetry-collector:0.164.0`, the core distribution
   (Apache-2.0), pinned by version and digest. OTLP/HTTP receiver, `memory_limiter` and `batch`
   processors, `debug` exporter to the collector's own log (`basic` by default, `detailed` on
   request). Published on `127.0.0.1:4318` only. Not started by default. The shared compose
   environment now passes `OTEL_ENABLED` (default `false`), `OTEL_EXPORTER` (default `none`),
   `OTEL_SAMPLE_RATE` and `OTEL_EXPORTER_OTLP_ENDPOINT` (default `http://otel-collector:4318`) to
   api, worker and beat; before this they could not be set for the containers at all.

## Alternatives considered
- **OTLP/gRPC** (`opentelemetry-exporter-otlp-proto-grpc`, or the `opentelemetry-exporter-otlp`
  meta-package that installs both): pulls in `grpcio`, a large native wheel, for no benefit at this
  volume. HTTP needs only `requests` and `protobuf`, which are already installed.
- **Jaeger or Tempo in the profile** for a trace UI: another service with storage on a 2-CPU, 7 GB
  laptop. The collector config is the single place to add one later; `debug` proves delivery.
- **File exporter**: only in the contrib distribution, which is several times larger.
- **A SpanProcessor that scrubs Redis spans on end**: finished spans are read-only in the SDK; the
  instrumentation hooks are the supported place.
- **Leave Celery untraced** (issue #209 rates it low value): no request enqueues tasks yet, but the
  scheduled jobs (payouts, outbox relay, drift) have no latency view at all, the dependency is from
  the same project and release line as the ones already locked, and it records no task arguments.

## Evidence (2026-11-04, 2-CPU laptop; load average 7-8 from other agents' work, so not a benchmark)
A throwaway collector (`docker run` of the pinned image on port 14318, `detailed` verbosity) received
from a scratch `uvicorn` (scratch database, Redis DB 9) and a solo Celery worker:
`POST /api/v1/auth/register`, `POST /api/v1/auth/login` and `GET /api/v1/accounts?token=...` as
SERVER spans with their SQL and Redis children (`INCRBY`, `EXPIRE`, no keys), and a
`pbip-api-beat` PRODUCER `apply_async/pbip.ping` whose child is the `pbip-api-worker` CONSUMER
`run/pbip.ping` in the same trace. The registered email, the password and the query-string value
appear nowhere in the collector output. With the collector stopped, 200 sequential
`GET /api/v1/accounts` all returned 200 (median 18.2 ms, p95 30.8 ms; 21.7 / 35.1 ms over 30 requests
with it running, the difference is noise at that load) while the API logged three
`Failed to export spans batch` warnings. The unit suite proves the same without timing noise against a
collector that accepts connections and never answers (`test_tracing_export.py`).

## Consequences
- Traces can be inspected end to end locally; nothing changes while `OTEL_ENABLED=false` (default).
- New dependencies, all Apache-2.0, from the OpenTelemetry project, same release lines as the
  locked ones (1.45.1 / 0.66b1): `opentelemetry-exporter-otlp-proto-http` (with its
  `-otlp-proto-common`, `-otlp-common`, `-http-transport` and `googleapis-common-protos`),
  `opentelemetry-instrumentation-celery`, `opentelemetry-instrumentation-redis`. No new external
  service and no data leaving the machine: the collector is local and only logs.
- Shutdown with a dead collector can take a few export timeouts while the last batches are tried.
- A pipeline that fails with an exception keeps the instrumentation's own `CMD ? ?` statement (its
  hook is not called on that path); it still contains no keys.
- Spans still carry client IP, user agent and UUID paths (ADR 0017, T33): a real collector must be
  access-controlled and must not forward to a third party without a new decision. Threat model T64.
- Production overhead with a network exporter (issue #209 item 4) is still unmeasured; it needs
  staging (#198).

## Standing-approval conditions
(1) No security control is weakened: tracing stays off by default, the endpoint is validated, keys
and task arguments never reach spans, `memory` is still refused when deployed. (2) No money path is
touched. (3) No schema change. (4) No external or paid service and no outbound data: the collector
runs locally and only logs; the dependencies are open source. (5) No fake data: the evidence above
is from real runs. (6) In scope: issue #209 and ADR 0017's roadmap.
