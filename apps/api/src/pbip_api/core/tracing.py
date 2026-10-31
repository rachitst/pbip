"""OpenTelemetry tracing for the API (TASK-085 spike, ADR 0017).

Off unless ``OTEL_ENABLED=true``. When on, FastAPI requests become SERVER spans and SQLAlchemy
statements on the shared engine become child CLIENT spans; ``core/logging.py`` adds the active
``trace_id``/``span_id`` to every log line. There is no network exporter yet (the production
collector is a roadmap item in ADR 0017), so turning tracing on never adds a network dependency.

Span data follows the access-log policy (``core/middleware.py``): no query strings, headers or
bodies. SQL statements carry bind placeholders only, and are never rewritten (no sqlcommenter).
"""

from typing import Any

import structlog
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, Sampler, TraceIdRatioBased
from opentelemetry.trace import Span
from sqlalchemy import Engine

from pbip_api import __version__
from pbip_api.core.config import Settings, get_settings

_logger = structlog.get_logger(__name__)

# Comma-separated regexes matched against the request URL. Health probes run every few seconds
# (compose healthchecks, nginx) and /metrics is scraped by Prometheus: tracing them is noise.
_EXCLUDED_URLS = "/api/health/,/metrics"

# One provider per process: the OpenTelemetry API allows the global provider to be set once.
_tracer_provider: TracerProvider | None = None
_in_memory_exporter: InMemorySpanExporter | None = None


def _build_sampler(sample_rate: float) -> Sampler:
    if sample_rate >= 1.0:
        return ALWAYS_ON
    if sample_rate <= 0.0:
        return ALWAYS_OFF
    return TraceIdRatioBased(sample_rate)


def setup_tracing(settings: Settings | None = None) -> TracerProvider | None:
    """Create and register the process-wide TracerProvider; idempotent, no-op when disabled."""
    global _tracer_provider, _in_memory_exporter

    settings = settings or get_settings()
    if not settings.otel_enabled:
        return None
    if _tracer_provider is not None:
        return _tracer_provider

    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": __version__,
            "deployment.environment": settings.app_env,
        }
    )
    # shutdown_on_exit (the default) flushes the batch processor at interpreter exit.
    provider = TracerProvider(resource=resource, sampler=_build_sampler(settings.otel_sample_rate))
    if settings.otel_exporter == "memory":
        # Test/evaluation only: keeps every span in memory (refused in staging/production).
        _in_memory_exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(_in_memory_exporter))
    elif settings.otel_exporter == "console":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
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


def get_in_memory_exporter() -> InMemorySpanExporter | None:
    """The in-memory exporter, when configured (tests and the overhead evaluation)."""
    return _in_memory_exporter


def get_active_trace_context() -> tuple[str | None, str | None]:
    """Return the active (trace_id, span_id) as hex strings, or (None, None) outside a span."""
    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        return f"{context.trace_id:032x}", f"{context.span_id:016x}"
    return None, None
