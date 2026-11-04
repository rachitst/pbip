"""OTLP export, Celery and Redis tracing (issue #209, ADR-0050).

The exporter must only ever cost dropped spans: these tests run the real app against a collector
that answers, one that accepts connections and never answers, and one that is not there.
"""

import socket
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from celery import Celery
from celery.contrib.testing.worker import start_worker
from fakeredis import FakeRedis
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, Decision
from opentelemetry.trace import SpanKind
from pbip_api.core import worker_metrics
from pbip_api.core.config import Settings
from pbip_api.core.db import utcnow
from pbip_api.core.ratelimit import clear_rate_limits, consume
from pbip_api.core.tracing import (
    OTLP_MAX_QUEUE_SIZE,
    _DropOrphanClientSpans,
    _keep_command_names_only,
    get_in_memory_exporter,
    instrument_redis,
    setup_celery_tracing,
    setup_tracing,
)
from pbip_api.core.velocity import VelocityCounters
from pbip_api.main import create_app
from pbip_api.worker import celery_app as celery_app_module
from pbip_test_support import reset_tracing
from pydantic import SecretStr, ValidationError

pytestmark = pytest.mark.unit

CLIENT_IP = "203.0.113.77"


@pytest.fixture(autouse=True)
def _clean_tracing() -> Iterator[None]:
    reset_tracing()
    yield
    reset_tracing()


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"otel_enabled": True, "otel_exporter": "memory"}
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]  # pydantic validates the values


def _batch_processor(provider: TracerProvider) -> BatchSpanProcessor:
    (processor,) = provider._active_span_processor._span_processors
    assert isinstance(processor, BatchSpanProcessor)
    return processor


class _Collector(ThreadingHTTPServer):
    """A minimal OTLP/HTTP receiver: keeps every /v1/traces body it is sent."""

    def __init__(self) -> None:
        self.bodies: list[bytes] = []
        self.paths: list[str] = []
        super().__init__(("127.0.0.1", 0), _CollectorHandler)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _CollectorHandler(BaseHTTPRequestHandler):
    server: _Collector

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.server.paths.append(self.path)
        self.server.bodies.append(self.rfile.read(length))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args: Any) -> None:  # Any: the stdlib signature
        return


@pytest.fixture
def collector() -> Iterator[_Collector]:
    server = _Collector()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


class _HangingCollector:
    """Accepts TCP connections and never answers: the worst case for a blocking exporter."""

    def __init__(self) -> None:
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._held: list[socket.socket] = []
        self.connected = threading.Event()
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self._listener.getsockname()[1]}"

    def _accept(self) -> None:
        while True:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            self._held.append(connection)
            self.connected.set()

    def close(self) -> None:
        self._listener.close()
        for connection in self._held:
            connection.close()


def _decoded(bodies: list[bytes]) -> list[tuple[str, dict[str, str], dict[str, str]]]:
    """(span name, span attributes, resource attributes) for every exported span."""
    spans = []
    for body in bodies:
        request = ExportTraceServiceRequest.FromString(body)
        for resource_spans in request.resource_spans:
            resource = {a.key: a.value.string_value for a in resource_spans.resource.attributes}
            for scope in resource_spans.scope_spans:
                for span in scope.spans:
                    attributes = {a.key: str(a.value) for a in span.attributes}
                    spans.append((span.name, attributes, resource))
    return spans


# --- configuration -------------------------------------------------------------------------------


def _deployed(**overrides: object) -> dict[str, object]:
    from cryptography.fernet import Fernet

    values: dict[str, object] = {
        "app_env": "production",
        "jwt_secret": SecretStr("s" * 48),
        "field_encryption_key": SecretStr(Fernet.generate_key().decode()),
        "cookie_secure": True,
        "cors_origins": "https://bank.example",
    }
    values.update(overrides)
    return values


def test_otlp_is_accepted_when_deployed_and_the_endpoint_is_fixed_configuration() -> None:
    settings = Settings(
        **_deployed(  # type: ignore[arg-type]
            otel_enabled=True,
            otel_exporter="otlp",
            otel_exporter_otlp_endpoint="http://otel-collector:4318",
        )
    )
    assert settings.otel_exporter == "otlp"
    fields = Settings.model_fields  # the code defaults, whatever the developer's shell exports
    assert fields["otel_exporter_otlp_endpoint"].default == "http://127.0.0.1:4318"
    assert fields["otel_export_timeout_seconds"].default == 2.0


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://collector:4318/v1/traces",  # a path: the exporter appends /v1/traces itself
        "http://user:pw@collector:4318",  # credentials  # pragma: allowlist secret
        "http://collector:4318?x=1",
        "grpc://collector:4317",
        "file:///etc/passwd",
        "",
    ],
)
def test_an_endpoint_other_than_scheme_host_port_is_refused(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        Settings(otel_exporter_otlp_endpoint=endpoint)


@pytest.mark.parametrize("timeout", [0, -1, 10.5])
def test_the_export_timeout_is_bounded(timeout: float) -> None:
    with pytest.raises(ValidationError):
        Settings(otel_export_timeout_seconds=timeout)


# --- OTLP export -----------------------------------------------------------------------------------


def test_spans_reach_the_collector_over_otlp_without_the_query_string(
    collector: _Collector,
) -> None:
    settings = _settings(otel_exporter="otlp", otel_exporter_otlp_endpoint=collector.endpoint)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/v1/accounts?token=abc123").status_code == 401
    provider = setup_tracing(settings)
    assert provider is not None
    assert provider.force_flush()

    assert collector.paths and set(collector.paths) == {"/v1/traces"}
    spans = _decoded(collector.bodies)
    (server,) = [s for s in spans if s[0] == "GET /api/v1/accounts"]
    _, attributes, resource = server
    assert resource["service.name"] == "pbip-api"
    assert "abc123" not in " ".join(attributes.values())


def test_the_otlp_queue_is_bounded() -> None:
    provider = setup_tracing(_settings(otel_exporter="otlp"))
    assert provider is not None
    processor = _batch_processor(provider)
    assert processor._batch_processor._queue.maxlen == OTLP_MAX_QUEUE_SIZE


def test_requests_do_not_wait_for_a_collector_that_never_answers() -> None:
    """The export thread is stuck on the network while requests keep completing at full speed."""
    hanging = _HangingCollector()
    timeout = 4.0
    try:
        settings = _settings(
            otel_exporter="otlp",
            otel_exporter_otlp_endpoint=hanging.endpoint,
            otel_export_timeout_seconds=timeout,
        )
        app = create_app(settings)
        provider = setup_tracing(settings)
        assert provider is not None
        with TestClient(app) as client:
            assert client.get("/api/v1/accounts").status_code == 401
            flush = threading.Thread(target=provider.force_flush, daemon=True)
            flush.start()
            assert hanging.connected.wait(10), "the exporter never tried to reach the collector"

            started = time.monotonic()
            statuses = [client.get("/api/v1/accounts").status_code for _ in range(20)]
            elapsed = time.monotonic() - started

        assert statuses == [401] * 20
        # 20 requests finish well inside one export timeout while an export is hanging; a
        # blocking exporter would hold the first of them for the whole timeout.
        assert elapsed < timeout, f"requests waited for the exporter ({elapsed:.2f}s)"
        flush.join(timeout * 3)
        assert not flush.is_alive(), "the export did not give up after its timeout"
    finally:
        hanging.close()


def test_requests_succeed_when_the_collector_is_down() -> None:
    with socket.create_server(("127.0.0.1", 0)) as placeholder:
        port = placeholder.getsockname()[1]
    # The port is closed now: every export is refused.
    settings = _settings(
        otel_exporter="otlp",
        otel_exporter_otlp_endpoint=f"http://127.0.0.1:{port}",
        otel_export_timeout_seconds=1.0,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        statuses = [client.get("/api/v1/accounts").status_code for _ in range(5)]
    assert statuses == [401] * 5
    provider = setup_tracing(settings)
    assert provider is not None
    provider.force_flush()  # the failed export is logged and dropped; it never raises


# --- sampling --------------------------------------------------------------------------------------


def test_a_client_span_without_a_parent_is_not_sampled() -> None:
    sampler = _DropOrphanClientSpans(ALWAYS_ON)
    assert sampler.should_sample(None, 1, "PING", SpanKind.CLIENT).decision == Decision.DROP
    for kind in (SpanKind.SERVER, SpanKind.PRODUCER, SpanKind.CONSUMER, SpanKind.INTERNAL):
        assert sampler.should_sample(None, 1, "x", kind).decision == Decision.RECORD_AND_SAMPLE

    provider = setup_tracing(_settings())
    assert provider is not None
    tracer = trace.get_tracer("test")
    with (
        tracer.start_as_current_span("request", kind=SpanKind.SERVER),
        tracer.start_as_current_span("SELECT", kind=SpanKind.CLIENT),
    ):
        pass
    with tracer.start_as_current_span("PING", kind=SpanKind.CLIENT):
        pass
    exporter = get_in_memory_exporter()
    assert exporter is not None
    assert [s.name for s in exporter.get_finished_spans()] == ["SELECT", "request"]


# --- Redis -----------------------------------------------------------------------------------------


def _all_text(spans: tuple[ReadableSpan, ...]) -> str:
    parts: list[str] = []
    for span in spans:
        parts.append(span.name)
        parts.extend(f"{k}={v}" for k, v in (span.attributes or {}).items())
        for event in span.events:
            parts.extend(f"{k}={v}" for k, v in (event.attributes or {}).items())
    return "\n".join(parts)


def test_redis_spans_carry_command_names_never_keys_or_client_ips() -> None:
    settings = _settings()
    setup_tracing(settings)
    redis_client = FakeRedis(decode_responses=True)
    instrument_redis(redis_client, settings)
    velocity = VelocityCounters(redis_client)
    now = utcnow()

    with trace.get_tracer("test").start_as_current_span("request", kind=SpanKind.SERVER):
        consume(redis_client, "login", CLIENT_IP, limit=10, window_seconds=60)
        velocity.record_login_failure(CLIENT_IP, uuid.uuid4(), now - timedelta(seconds=1))
        assert velocity.login_failures_from_ip(CLIENT_IP, now) == 1
        clear_rate_limits(redis_client, ["login"], [CLIENT_IP])

    exporter = get_in_memory_exporter()
    assert exporter is not None
    spans = exporter.get_finished_spans()
    redis_spans = [s for s in spans if s.kind == SpanKind.CLIENT]
    statements = [(s.attributes or {}).get("db.statement") for s in redis_spans]
    assert statements == ["INCRBY", "EXPIRE", "ZADD\nZREMRANGEBYSCORE\nEXPIRE", "ZRANGEBYSCORE",
                          "DEL"]  # fmt: skip
    text = _all_text(spans)
    assert CLIENT_IP not in text
    assert "rl:" not in text
    assert "?" not in "".join(str(s) for s in statements)


def test_the_hook_strips_keys_even_if_a_release_starts_recording_them() -> None:
    setup_tracing(_settings())
    with trace.get_tracer("test").start_as_current_span("x", kind=SpanKind.SERVER) as span:
        span.set_attribute("db.statement", f"INCR rl:login:{CLIENT_IP}")
        span.set_attribute("db.query.text", f"ZADD vel:login:{CLIENT_IP} 1 a\nEXPIRE k 60")
        _keep_command_names_only(span, None, ("INCR",), {})
    exporter = get_in_memory_exporter()
    assert exporter is not None
    (finished,) = exporter.get_finished_spans()
    assert (finished.attributes or {})["db.statement"] == "INCR"
    assert (finished.attributes or {})["db.query.text"] == "ZADD\nEXPIRE"


def test_the_redis_client_is_instrumented_only_when_tracing_is_on() -> None:
    off = FakeRedis()
    instrument_redis(off, Settings())
    assert not hasattr(off.execute_command, "__wrapped__")
    setup_tracing(_settings())
    on = FakeRedis()
    instrument_redis(on, _settings())
    assert hasattr(on.execute_command, "__wrapped__")


# --- Celery ----------------------------------------------------------------------------------------


def test_the_worker_signal_traces_the_worker_under_its_own_service_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pbip_api.core.tracing.get_settings", _settings)
    celery_app_module._trace_worker_process()
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    assert provider.resource.attributes["service.name"] == "pbip-api-worker"


def test_a_task_joins_the_trace_that_enqueued_it_without_its_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The embedded worker sends worker_init, which would mark this test process as a worker.
    monkeypatch.setitem(worker_metrics._role, "worker", False)
    provider = setup_celery_tracing("worker", _settings())
    assert provider is not None
    app = Celery("otel-test", broker="memory://", backend="cache+memory://")
    app.conf.update(task_serializer="json", accept_content=["json"], result_serializer="json")

    @app.task(name="otel.echo")
    def echo(value: str) -> int:
        return len(value)

    marker = "task-argument-9f3c"
    with start_worker(app, pool="threads", perform_ping_check=False, shutdown_timeout=10):
        with trace.get_tracer("test").start_as_current_span("enqueue") as parent:
            result = echo.delay(marker)
        assert result.get(timeout=20) == len(marker)

    exporter = get_in_memory_exporter()
    assert exporter is not None
    spans = exporter.get_finished_spans()
    (publish,) = [s for s in spans if s.kind == SpanKind.PRODUCER]
    (run,) = [s for s in spans if s.kind == SpanKind.CONSUMER]
    trace_id = parent.get_span_context().trace_id
    assert publish.context.trace_id == trace_id
    # The worker continued the trace from the traceparent in the task headers.
    assert run.context.trace_id == trace_id
    assert run.parent is not None and run.parent.span_id == publish.context.span_id
    assert marker not in _all_text(spans)
