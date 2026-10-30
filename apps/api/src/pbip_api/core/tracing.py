"""OpenTelemetry distributed tracing configuration, instrumentation, and utilities (TASK-085).

Provides centralized setup for OpenTelemetry span tracking across FastAPI request lifecycles
and SQLAlchemy database operations, injecting trace_id and span_id into structlog logging context.
"""

import contextlib
from typing import Any

import structlog
from fastapi import FastAPI
from sqlalchemy import Engine

from pbip_api.core.config import Settings, get_settings

try:
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
    from opentelemetry.sdk.trace.sampling import (
        ALWAYS_OFF,
        ALWAYS_ON,
        TraceIdRatioBased,
    )

    _HAS_OTEL = True
except ImportError:  # pragma: no cover
    _HAS_OTEL = False

_logger = structlog.get_logger(__name__)

_in_memory_exporter: Any = None
_tracer_provider: Any = None
_instrumented_engines: set[int] = set()
_instrumented_apps: set[int] = set()


def _build_sampler(sample_rate: float) -> Any:
    if not _HAS_OTEL:
        return None
    if sample_rate >= 1.0:
        return ALWAYS_ON
    if sample_rate <= 0.0:
        return ALWAYS_OFF
    return TraceIdRatioBased(sample_rate)


def setup_tracing(
    settings: Settings | None = None,
) -> tuple[Any, Any]:
    """Configure the OpenTelemetry TracerProvider and exporter based on settings."""
    global _tracer_provider, _in_memory_exporter

    if not _HAS_OTEL:
        return None, None

    settings = settings or get_settings()
    if not settings.otel_enabled:
        return None, None

    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": "0.1.0",
            "deployment.environment": settings.app_env,
        }
    )
    sampler = _build_sampler(settings.otel_sample_rate)
    provider = TracerProvider(resource=resource, sampler=sampler)

    exporter: Any = None
    if settings.otel_exporter == "memory":
        exporter = InMemorySpanExporter()
        _in_memory_exporter = exporter
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    elif settings.otel_exporter == "console":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    elif settings.otel_exporter == "none":
        pass

    trace.set_tracer_provider(provider)
    _tracer_provider = provider
    return provider, exporter


def instrument_app(app: FastAPI, settings: Settings | None = None) -> None:
    """Instrument a FastAPI application for distributed trace context propagation."""
    global _instrumented_apps

    if not _HAS_OTEL:
        return

    settings = settings or get_settings()
    if not settings.otel_enabled:
        return

    app_id = id(app)
    if app_id in _instrumented_apps:
        return

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=_tracer_provider,
        excluded_urls="metrics",
        exclude_spans=["send", "receive"],
    )
    _instrumented_apps.add(app_id)


def instrument_engine(engine: Engine, settings: Settings | None = None) -> None:
    """Instrument a SQLAlchemy engine to create child spans for SQL statements."""
    global _instrumented_engines

    if not _HAS_OTEL:
        return

    settings = settings or get_settings()
    if not settings.otel_enabled:
        return

    engine_id = id(engine)
    if engine_id in _instrumented_engines:
        return

    SQLAlchemyInstrumentor().instrument(
        engine=engine,
        tracer_provider=_tracer_provider,
        skip_dep_check=True,
        enable_commenter=True,
    )
    _instrumented_engines.add(engine_id)


def get_in_memory_exporter() -> Any:
    """Retrieve the active InMemorySpanExporter (primarily used in tests and evaluations)."""
    return _in_memory_exporter


def get_active_trace_context() -> tuple[str | None, str | None]:
    """Return active (trace_id, span_id) hex strings if currently within a valid span."""
    if not _HAS_OTEL:
        return None, None
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.is_valid:
        return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"
    return None, None


def uninstrument_tracing() -> None:
    """Uninstrument components and reset global tracing state (used for test isolation)."""
    global _tracer_provider, _in_memory_exporter, _instrumented_engines, _instrumented_apps

    if _HAS_OTEL:
        with contextlib.suppress(Exception):
            if SQLAlchemyInstrumentor().is_instrumented_by_opentelemetry:
                SQLAlchemyInstrumentor().uninstrument()
        with contextlib.suppress(Exception):
            trace._TRACER_PROVIDER = None
            trace._TRACER_PROVIDER_SET_ONCE._done = False
        _in_memory_exporter = None
        _tracer_provider = None
        _instrumented_engines.clear()
        _instrumented_apps.clear()
