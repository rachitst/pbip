"""OpenTelemetry tracing for the API (TASK-085 spike, ADR 0017).

Off unless ``OTEL_ENABLED=true``. When on, FastAPI requests become SERVER spans and SQLAlchemy
statements on the shared engine become child CLIENT spans; ``core/logging.py`` adds the active
``trace_id``/``span_id`` to every log line. With ``OTEL_EXPORTER=otlp`` spans go to an
OpenTelemetry Collector over OTLP/HTTP through a bounded queue and an export timeout, so a collector
that is down or hanging costs dropped spans, never a slower or failed request (ADR-0050). Celery
workers and beat (``setup_celery_tracing``) and the application's Redis client
(``instrument_redis``) are traced too; Redis spans carry command names only, never keys.

Span data follows the access-log policy (``core/middleware.py``): no query strings, headers or
bodies. SQL statements carry bind placeholders only, and are never rewritten (no sqlcommenter).
"""

from collections.abc import Sequence
from typing import Any

import structlog
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.celery import CeleryInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    Decision,
    Sampler,
    SamplingResult,
    TraceIdRatioBased,
)
from opentelemetry.trace import Link, Span, SpanKind
from opentelemetry.trace.span import TraceState
from opentelemetry.util.types import Attributes
from redis import Redis
from sqlalchemy import Engine

from pbip_api import __version__
from pbip_api.core.config import Settings, get_settings

_logger = structlog.get_logger(__name__)

# Comma-separated regexes, searched (not matched) by the ASGI instrumentation against
# "scheme://host/path", so each is anchored to the start of the path: an unanchored "/metrics"
# would also silence any future route that merely contains it. Health probes run every few seconds
# (compose healthchecks, nginx) and /metrics is scraped by Prometheus: tracing them is noise.
_EXCLUDED_URLS = r"^[a-z]+://[^/]+/api/health/,^[a-z]+://[^/]+/metrics$"

# OTLP batching (ADR-0050). Finished spans wait in a queue of at most this many and are sent in
# batches every few seconds by the processor's own thread; when the queue is full, new spans are
# dropped (the SDK logs it), so memory stays bounded however long the collector is away.
OTLP_MAX_QUEUE_SIZE = 2048
OTLP_MAX_EXPORT_BATCH_SIZE = 512
OTLP_SCHEDULE_DELAY_MILLIS = 5000

# Span attributes where the Redis instrumentation writes the command. It already replaces every
# argument with "?"; the hooks below keep only the command names, so a key (rate-limit keys embed
# the client IP: core/ratelimit.py) can never reach a span, whatever a later release records.
_REDIS_STATEMENT_ATTRIBUTES = ("db.statement", "db.query.text")

# One provider per process: the OpenTelemetry API allows the global provider to be set once.
_tracer_provider: TracerProvider | None = None
_in_memory_exporter: InMemorySpanExporter | None = None


def _build_sampler(sample_rate: float) -> Sampler:
    if sample_rate >= 1.0:
        return ALWAYS_ON
    if sample_rate <= 0.0:
        return ALWAYS_OFF
    return TraceIdRatioBased(sample_rate)


class _DropOrphanClientSpans(Sampler):
    """Never sample a CLIENT span that has no parent; defer to ``delegate`` otherwise.

    SQL and Redis calls made outside a request or task (readiness probes every few seconds, the
    Prometheus scrape reading worker metrics from Redis) would each become a one-span trace and
    flood the collector. Inside a request or a Celery task they are children and are kept.
    """

    def __init__(self, delegate: Sampler) -> None:
        self._delegate = delegate

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        parent = trace.get_current_span(parent_context).get_span_context()
        if kind == SpanKind.CLIENT and not parent.is_valid:
            return SamplingResult(Decision.DROP)
        return self._delegate.should_sample(
            parent_context, trace_id, name, kind, attributes, links, trace_state
        )

    def get_description(self) -> str:
        return f"DropOrphanClientSpans{{{self._delegate.get_description()}}}"


def _otlp_processor(settings: Settings) -> BatchSpanProcessor:
    """OTLP/HTTP export that can only ever cost dropped spans (ADR-0050).

    ``on_end`` (on the request thread) only appends to the bounded queue; the network call runs on
    the processor's own thread. The exporter's ``timeout`` is the bound on one export, retries
    included (the SDK's ``export_timeout_millis`` is not applied by this SDK release, so it is not
    relied on); after it the batch is dropped.
    """
    exporter = OTLPSpanExporter(
        endpoint=settings.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces",
        timeout=settings.otel_export_timeout_seconds,
    )
    return BatchSpanProcessor(
        exporter,
        max_queue_size=OTLP_MAX_QUEUE_SIZE,
        schedule_delay_millis=OTLP_SCHEDULE_DELAY_MILLIS,
        max_export_batch_size=OTLP_MAX_EXPORT_BATCH_SIZE,
    )


def setup_tracing(
    settings: Settings | None = None, *, service_name: str | None = None
) -> TracerProvider | None:
    """Create and register the process-wide TracerProvider; idempotent, no-op when disabled.

    ``service_name`` overrides ``OTEL_SERVICE_NAME`` for Celery processes, so worker and beat
    spans are told apart from the API's in the collector.
    """
    global _tracer_provider, _in_memory_exporter

    settings = settings or get_settings()
    if not settings.otel_enabled:
        return None
    if _tracer_provider is not None:
        return _tracer_provider

    resource = Resource.create(
        {
            "service.name": service_name or settings.otel_service_name,
            "service.version": __version__,
            "deployment.environment": settings.app_env,
        }
    )
    # shutdown_on_exit (the default) flushes the batch processor at interpreter exit.
    provider = TracerProvider(
        resource=resource,
        sampler=_DropOrphanClientSpans(_build_sampler(settings.otel_sample_rate)),
    )
    if settings.otel_exporter == "memory":
        # Test/evaluation only: keeps every span in memory (refused in staging/production).
        _in_memory_exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(_in_memory_exporter))
    elif settings.otel_exporter == "console":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    elif settings.otel_exporter == "otlp":
        provider.add_span_processor(_otlp_processor(settings))
    # "none": spans are created (so trace ids reach the logs) but not exported.

    trace.set_tracer_provider(provider)
    _tracer_provider = provider
    _logger.info(
        "tracing_enabled",
        exporter=settings.otel_exporter,
        sample_rate=settings.otel_sample_rate,
    )
    return provider


def _strip_query_string(span: Span, _scope: dict[str, Any]) -> None:
    """Server request hook: drop the query string the ASGI instrumentation records.

    Query values can hold identifiers or tokens; the access log omits them, so spans do too.
    """
    if not span.is_recording():
        return
    attributes = getattr(span, "attributes", None) or {}
    for key in ("http.url", "url.full", "http.target"):
        value = attributes.get(key)
        if isinstance(value, str) and "?" in value:
            span.set_attribute(key, value.split("?", 1)[0])
    if "url.query" in attributes:
        span.set_attribute("url.query", "")


def instrument_app(app: FastAPI, settings: Settings | None = None) -> None:
    """Wrap a FastAPI app so each request is a SERVER span (W3C traceparent is honoured)."""
    settings = settings or get_settings()
    if not settings.otel_enabled or _tracer_provider is None:
        return
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=_tracer_provider,
        excluded_urls=_EXCLUDED_URLS,
        exclude_spans=["send", "receive"],
        server_request_hook=_strip_query_string,
    )


def instrument_engine(engine: Engine, settings: Settings | None = None) -> None:
    """Add CLIENT spans for SQL on this engine.

    ``skip_dep_check``: the instrumentation declares support for SQLAlchemy < 2.1 and PBIP runs
    2.1.x; the engine-event hooks it uses are unchanged in 2.1 (covered by the integration test
    against PostgreSQL). Revisit when a release declares 2.1 support (ADR 0017).
    """
    settings = settings or get_settings()
    if not settings.otel_enabled:
        return
    SQLAlchemyInstrumentor().instrument(
        engine=engine,
        tracer_provider=_tracer_provider,
        skip_dep_check=True,
        enable_commenter=False,
    )


def setup_celery_tracing(role: str, settings: Settings | None = None) -> TracerProvider | None:
    """Trace a Celery process (``role`` is ``worker`` or ``beat``); no-op when disabled.

    Called from ``worker_process_init`` (after the prefork fork, so the export thread lives in the
    child) and ``beat_init``. The instrumentation injects W3C ``traceparent`` into task headers on
    publish and continues it in the worker, so a task's spans join the trace that enqueued it. It
    records task names, ids and routing, never task arguments.
    """
    settings = settings or get_settings()
    provider = setup_tracing(settings, service_name=f"{settings.otel_service_name}-{role}")
    if provider is None:
        return None
    # The package leaves __init__ unannotated (0.66b1); it takes no arguments.
    instrumentor = CeleryInstrumentor()  # type: ignore[no-untyped-call]
    if not instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.instrument(tracer_provider=provider)
    return provider


def _keep_command_names_only(span: Span, *_hook_args: object) -> None:
    """Redis request/response hook: reduce the recorded statement to its command names."""
    if not span.is_recording():
        return
    attributes = getattr(span, "attributes", None) or {}
    for key in _REDIS_STATEMENT_ATTRIBUTES:
        value = attributes.get(key)
        if isinstance(value, str):
            names = (line.split(" ", 1)[0] for line in value.splitlines())
            span.set_attribute(key, "\n".join(names))


def instrument_redis(client: Redis, settings: Settings | None = None) -> None:
    """Add CLIENT spans for this Redis client's commands (not Celery's broker connections).

    Only this client is instrumented: a global ``RedisInstrumentor().instrument()`` would also
    trace the broker's blocking polls in every worker, once a second.
    """
    settings = settings or get_settings()
    if not settings.otel_enabled:
        return
    RedisInstrumentor.instrument_client(
        client,
        tracer_provider=_tracer_provider,
        request_hook=_keep_command_names_only,
        response_hook=_keep_command_names_only,
    )


def get_in_memory_exporter() -> InMemorySpanExporter | None:
    """The in-memory exporter, when configured (tests and the overhead evaluation)."""
    return _in_memory_exporter


def get_active_trace_context() -> tuple[str | None, str | None]:
    """Return the active (trace_id, span_id) as hex strings, or (None, None) outside a span."""
    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        return f"{context.trace_id:032x}", f"{context.span_id:016x}"
    return None, None
