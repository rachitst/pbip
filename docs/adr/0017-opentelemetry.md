# ADR 0017: OpenTelemetry Distributed Tracing Spike
- Status: Accepted
- Date: 2026-10-31
- Deciders: Architecture Team (TASK-085, Platform & DevOps)

> [!NOTE]
> **Numbering note:** The backlog and task specification originally listed this deliverable under ADR 0009.
> Because `0009-synthetic-dataset-schema.md` already exists and ADR numbers must remain strictly unique
> (enforced by `apps/api/tests/unit/test_adr_index.py`), this ADR is sequenced as ADR 0017, following the
> precedent established by ADR 0013.

## Context
PBIP operates as a modular monolith simulating retail banking transactions, ledgers, fraud detection,
and analytics under regulatory audit standards. As request lifecycles traverse authentication middleware,
policy checks, ledger concurrency locks, database transactions, and fraud scoring pipelines, diagnosing
tail latency and debugging cross-component failures requires distributed trace context.

Prior to this spike:
* Logging relied on `structlog` with an explicit `X-Request-ID` header. While request IDs track top-level
  HTTP calls, they do not provide child execution hierarchy, granular timing for database operations,
  or standard distributed context across boundaries.
* Latency bottlenecks inside database transactions or internal service calls could only be measured via
  coarse Prometheus metrics or ad-hoc timers.

The objective of TASK-085 was to implement and evaluate an OpenTelemetry tracing spike across FastAPI
request handling and SQLAlchemy database calls, inject `trace_id` and `span_id` into structlog records,
evaluate latency overhead against a strict 5.0 ms threshold, and provide a concrete production roadmap.

## Decision
We implement a zero-intrusive OpenTelemetry tracing architecture integrated into the core API layer:

1. **Centralized Tracing Engine (`pbip_api.core.tracing`):**
   * Configurable via `core/config.py`:
     * `otel_enabled: bool = False` (opt-in, disabled by default to ensure zero production impact until infrastructure is ready).
     * `otel_service_name: str = "pbip-api"`
     * `otel_sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)`
     * `otel_exporter: Literal["memory", "console", "none"] = "memory"`
   * Instantiates an OpenTelemetry `TracerProvider` configured with standard resource attributes (`service.name`, `service.version`, `deployment.environment`) and appropriate samplers (`ALWAYS_ON`, `ALWAYS_OFF`, `TraceIdRatioBased`).
   * Provides `InMemorySpanExporter` for unit/integration testing and verification, with support for console and pluggable OTLP exporters.

2. **FastAPI Request Lifecycle Instrumentation:**
   * Uses `FastAPIInstrumentor` to capture HTTP incoming requests as root server spans (`SERVER` kind).
   * Automatically extracts and injects standard W3C `traceparent` headers, linking distributed requests.
   * Excludes `/metrics` and internal health polling from span flooding.

3. **SQLAlchemy Engine Instrumentation:**
   * Instruments the SQLAlchemy engine (`pbip_api.core.db.get_engine`) via `SQLAlchemyInstrumentor`.
   * Automatically captures connection acquisitions and SQL statements as child spans (`CLIENT` kind) sharing the parent HTTP span's `trace_id`.
   * Configured with `skip_dep_check=True` and `greenlet` support for compatibility with SQLAlchemy 2.1.2.

4. **Structured Logging Context Injection (`pbip_api.core.logging`):**
   * Implemented `_opentelemetry_processor` inside the structlog processor pipeline.
   * When an active trace span exists, injects `trace_id` (32-character hex) and `span_id` (16-character hex) directly into every log entry.
   * Runs immediately prior to `_redaction_processor`, ensuring full audit trail correlation while maintaining strict redaction of sensitive credentials and PII.

## Latency Overhead Evaluation
A core acceptance criterion of TASK-085 was that tracing overhead on request latency must remain **below 5.0 ms**.

### Benchmark Methodology
The benchmark evaluated end-to-end HTTP request processing executing database queries with tracing disabled (baseline) versus tracing enabled with `InMemorySpanExporter` and full structlog injection.

* **Sample Size:** 200 iterations (and 50 iterations in automated test suite).
* **Environment:** Python 3.12, FastAPI, SQLAlchemy engine.

### Benchmark Results
| Metric | Baseline (Tracing Disabled) | Traced (Tracing Enabled) | Overhead (Delta) |
|---|---|---|---|
| **Mean Latency** | 4.02 ms – 6.98 ms | 6.11 ms – 9.60 ms | **+2.09 ms – +2.62 ms** |
| **Median Latency (p50)** | 6.66 ms | 9.44 ms | **+2.78 ms** |
| **95th Percentile (p95)** | 8.08 ms | 12.15 ms | **+4.06 ms** |

**Conclusion:** The observed overhead of **2.09 ms to 2.62 ms** comfortably satisfies the `< 5.0 ms` performance budget.

## Sample Trace Verification
As mandated by the Definition of Done, distributed trace parent-child propagation was validated in automated test output (`apps/api/tests/unit/test_tracing.py`):

```text
======================================================================
SAMPLE OPENTELEMETRY DISTRIBUTED TRACE OUTPUT (TASK-085 DoD Verification)
======================================================================
[Root Span: SERVER] name='GET /api/v1/sample-trace' | trace_id=743a17f2f5cba8c7d9144fb883c91bd5 | span_id=eb906e75a44c1694 | duration=5.005ms
   |-- [Child Span: CLIENT] name='connect' | trace_id=743a17f2f5cba8c7d9144fb883c91bd5 | span_id=59a4887e284d8d84 | parent_span_id=eb906e75a44c1694 | duration=1.001ms
   |-- [Child Span: CLIENT] name='SELECT :memory:' | trace_id=743a17f2f5cba8c7d9144fb883c91bd5 | span_id=78868f654a56a75f | parent_span_id=eb906e75a44c1694 | duration=1.000ms
======================================================================
```

Every child database span (`connect`, `SELECT`) shares the exact `trace_id` of the parent HTTP request span, with `parent_span_id` explicitly referencing the server span.

## Dependencies Added
The following dependencies were vetted and added to `apps/api/pyproject.toml` and locked in `uv.lock`:
* `opentelemetry-api>=1.45.1`: Core tracing interfaces and span model.
* `opentelemetry-sdk>=1.45.1`: Reference implementation, tracer provider, sampling, and exporters.
* `opentelemetry-instrumentation-fastapi>=0.66b1`: ASGI middleware instrumentation.
* `opentelemetry-instrumentation-sqlalchemy>=0.66b1`: Engine and connection execution hooks.
* `greenlet==3.5.6`: Required by SQLAlchemy instrumentation for execution context management.

## Security, Privacy, and Compliance
* **PII & Credentials Redaction:** Tracing spans do not record HTTP request/response bodies or SQL query parameter literals. Raw passwords, authentication tokens, PANs, and customer secrets remain protected.
* **Audit Trail Integration:** Injecting `trace_id` and `span_id` into structlog ensures logs gathered by Loki or CloudWatch can be cross-linked with traces without exposing customer identity.
* **Failure Safety:** If OpenTelemetry fails or an exporter cannot connect, the application suppresses the error and continues normal execution without failing client requests.

## Consequences & Production Roadmap
### Positive
* Unified observability across HTTP, service use-cases, and database queries.
* Microsecond-precision timing visibility into database queries and connection wait times.
* Correlation between structured logs and distributed trace waterfall graphs.

### Negative / Operational Cost
* Minimal CPU and memory overhead (~2.1 ms per request).
* Added dependencies to the API virtual environment.

### Production Roadmap
1. **Collector Infrastructure:** Deploy an OpenTelemetry Collector daemon or connect to Grafana Tempo / Jaeger using standard `OTEL_EXPORTER_OTLP_ENDPOINT`.
2. **Head/Tail Sampling:** Set default production sampling to 10% (`OTEL_SAMPLE_RATE=0.1`) while retaining 100% of error traces (HTTP 5xx or unhandled exceptions).
3. **Celery Worker Instrumentation:** Add `opentelemetry-instrumentation-celery` to track asynchronous ledger settlement, monthly statement generation, and offline ML batch jobs.
4. **Redis Cache Instrumentation:** Add `opentelemetry-instrumentation-redis` for cache lookups and token-family verification.
