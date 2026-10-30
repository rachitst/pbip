"""Unit tests and evaluation for OpenTelemetry distributed tracing spike (TASK-085).

Covers:
- OpenTelemetry setup, configuration, and sampler configuration
- FastAPI request lifecycle tracing
- SQLAlchemy query execution child spans linked to HTTP parent span
- Structlog trace context injection (trace_id, span_id)
- Definition of Done verification: sample trace visibility in test output
- Latency overhead evaluation (< 5.0 ms target)
"""

import sys
import time
from collections.abc import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.core.logging import _opentelemetry_processor
from pbip_api.core.tracing import (
    _HAS_OTEL,
    get_active_trace_context,
    get_in_memory_exporter,
    instrument_app,
    instrument_engine,
    setup_tracing,
    uninstrument_tracing,
)
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clean_tracing_state() -> Generator[None, None, None]:
    """Ensure tracing instrumentation is cleaned up before and after each test."""
    uninstrument_tracing()
    yield
    uninstrument_tracing()


def test_tracing_disabled_by_default() -> None:
    settings = Settings(otel_enabled=False)
    provider, exporter = setup_tracing(settings)
    assert provider is None
    assert exporter is None
    assert get_in_memory_exporter() is None


def test_tracing_setup_enabled_memory_exporter() -> None:
    settings = Settings(
        otel_enabled=True,
        otel_service_name="pbip-api-test",
        otel_sample_rate=1.0,
        otel_exporter="memory",
    )
    provider, exporter = setup_tracing(settings)
    assert provider is not None
    assert exporter is not None
    assert get_in_memory_exporter() is exporter


def test_structlog_trace_context_injection_outside_span() -> None:
    event_dict = {"event": "test message", "level": "info"}
    processed = _opentelemetry_processor(None, "info", event_dict)
    assert "trace_id" not in processed
    assert "span_id" not in processed
    assert processed["event"] == "test message"

    trace_id, span_id = get_active_trace_context()
    assert trace_id is None
    assert span_id is None


def test_structlog_trace_context_injection_inside_span() -> None:
    if not _HAS_OTEL:
        pytest.skip("OpenTelemetry is not installed")

    from opentelemetry import trace

    settings = Settings(otel_enabled=True, otel_exporter="memory")
    setup_tracing(settings)

    tracer = trace.get_tracer("test-tracer")
    with tracer.start_as_current_span("test-operation") as span:
        ctx = span.get_span_context()
        assert ctx.is_valid

        active_tid, active_sid = get_active_trace_context()
        assert active_tid == f"{ctx.trace_id:032x}"
        assert active_sid == f"{ctx.span_id:016x}"

        event_dict = {"event": "database query executed"}
        processed = _opentelemetry_processor(None, "info", event_dict)
        assert processed["trace_id"] == f"{ctx.trace_id:032x}"
        assert processed["span_id"] == f"{ctx.span_id:016x}"


def test_fastapi_and_sqlalchemy_distributed_trace_propagation() -> None:
    if not _HAS_OTEL:
        pytest.skip("OpenTelemetry is not installed")

    settings = Settings(otel_enabled=True, otel_exporter="memory")
    setup_tracing(settings)
    exporter = get_in_memory_exporter()
    assert exporter is not None

    test_engine = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    instrument_engine(test_engine, settings)

    app = FastAPI()
    instrument_app(app, settings)

    @app.get("/api/v1/test-tracer")
    def sample_endpoint() -> dict[str, str]:
        with test_engine.connect() as conn:
            result = conn.execute(text("SELECT 42 AS value")).scalar()
        return {"result": str(result)}

    with TestClient(app) as client:
        response = client.get("/api/v1/test-tracer")
        assert response.status_code == 200
        assert response.json() == {"result": "42"}

    finished_spans = exporter.get_finished_spans()
    assert len(finished_spans) >= 2, f"Expected at least 2 spans, got {len(finished_spans)}"

    server_spans = [s for s in finished_spans if s.kind.name == "SERVER"]
    assert len(server_spans) == 1
    server_span = server_spans[0]
    expected_trace_id = f"{server_span.context.trace_id:032x}"

    db_spans = [s for s in finished_spans if s != server_span]
    assert len(db_spans) >= 1

    for db_span in db_spans:
        db_trace_id = f"{db_span.context.trace_id:032x}"
        assert db_trace_id == expected_trace_id, (
            f"DB span {db_span.name} trace_id {db_trace_id} does not match server trace_id {expected_trace_id}"
        )


def test_sample_trace_visible_in_test_output(capsys: pytest.CaptureFixture[str]) -> None:
    """Verifies DoD requirement: 'Verify that a sample trace is visible in test output'."""
    if not _HAS_OTEL:
        pytest.skip("OpenTelemetry is not installed")

    settings = Settings(otel_enabled=True, otel_exporter="memory")
    setup_tracing(settings)
    exporter = get_in_memory_exporter()
    assert exporter is not None

    test_engine = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    instrument_engine(test_engine, settings)

    app = FastAPI()
    instrument_app(app, settings)

    @app.get("/api/v1/sample-trace")
    def sample_trace_endpoint() -> dict[str, str]:
        with test_engine.connect() as conn:
            conn.execute(text("SELECT 100"))
        return {"status": "ok"}

    with TestClient(app) as client:
        response = client.get("/api/v1/sample-trace")
        assert response.status_code == 200

    spans = exporter.get_finished_spans()
    assert len(spans) >= 2

    # Write formatted sample trace tree to stdout as mandated by DoD
    lines = [
        "\n" + "=" * 70,
        "SAMPLE OPENTELEMETRY DISTRIBUTED TRACE OUTPUT (TASK-085 DoD Verification)",
        "=" * 70,
    ]
    root_span = next(s for s in spans if s.parent is None or s.kind.name == "SERVER")
    root_tid = f"{root_span.context.trace_id:032x}"
    root_sid = f"{root_span.context.span_id:016x}"
    duration_ms = (root_span.end_time - root_span.start_time) / 1_000_000
    lines.append(
        f"[Root Span: {root_span.kind.name}] name='{root_span.name}' | trace_id={root_tid} | "
        f"span_id={root_sid} | duration={duration_ms:.3f}ms"
    )

    for child_span in spans:
        if child_span == root_span:
            continue
        c_tid = f"{child_span.context.trace_id:032x}"
        c_sid = f"{child_span.context.span_id:016x}"
        c_pid = f"{child_span.parent.span_id:016x}" if child_span.parent else "None"
        c_dur = (child_span.end_time - child_span.start_time) / 1_000_000
        lines.append(
            f"   |-- [Child Span: {child_span.kind.name}] name='{child_span.name}' | "
            f"trace_id={c_tid} | span_id={c_sid} | parent_span_id={c_pid} | duration={c_dur:.3f}ms"
        )
    lines.append("=" * 70 + "\n")

    output_str = "\n".join(lines)
    sys.stdout.write(output_str + "\n")

    captured = capsys.readouterr()
    assert "SAMPLE OPENTELEMETRY DISTRIBUTED TRACE OUTPUT" in captured.out
    assert root_tid in captured.out
    sys.stdout.write(captured.out)


def test_tracing_latency_overhead_under_five_milliseconds() -> None:
    """Verifies DoD requirement: 'Evaluate tracing overhead on request latency (<5ms)'."""
    iterations = 50

    # 1. Baseline latency (tracing disabled)
    uninstrument_tracing()
    engine_baseline = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    app_baseline = FastAPI()

    @app_baseline.get("/benchmark")
    def benchmark_baseline() -> dict[str, str]:
        with engine_baseline.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok"}

    baseline_times: list[float] = []
    with TestClient(app_baseline) as client_baseline:
        # Warmup
        for _ in range(5):
            client_baseline.get("/benchmark")
        for _ in range(iterations):
            start = time.perf_counter()
            resp = client_baseline.get("/benchmark")
            assert resp.status_code == 200
            baseline_times.append((time.perf_counter() - start) * 1000.0)

    # 2. Traced latency (tracing enabled)
    settings = Settings(otel_enabled=True, otel_exporter="memory")
    setup_tracing(settings)
    engine_traced = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    instrument_engine(engine_traced, settings)
    app_traced = FastAPI()
    instrument_app(app_traced, settings)

    @app_traced.get("/benchmark")
    def benchmark_traced() -> dict[str, str]:
        with engine_traced.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok"}

    traced_times: list[float] = []
    with TestClient(app_traced) as client_traced:
        # Warmup
        for _ in range(5):
            client_traced.get("/benchmark")
        for _ in range(iterations):
            start = time.perf_counter()
            resp = client_traced.get("/benchmark")
            assert resp.status_code == 200
            traced_times.append((time.perf_counter() - start) * 1000.0)

    mean_baseline = sum(baseline_times) / len(baseline_times)
    mean_traced = sum(traced_times) / len(traced_times)
    overhead = mean_traced - mean_baseline

    benchmark_summary = (
        f"\nLatency Benchmark (N={iterations}):\n"
        f"  Baseline Mean: {mean_baseline:.3f} ms\n"
        f"  Traced Mean:   {mean_traced:.3f} ms\n"
        f"  Overhead:      {overhead:.3f} ms\n"
    )
    sys.stdout.write(benchmark_summary)

    # Assert latency overhead is under the 5 ms target threshold
    assert overhead < 5.0, f"Tracing overhead {overhead:.3f} ms exceeds 5.0 ms threshold"
