"""OpenTelemetry tracing spike (TASK-085, ADR 0017): configuration, wiring and log correlation.

The database half (SQL child spans against PostgreSQL) is in tests/integration/test_tracing.py.
"""

import json
import logging
from collections.abc import Iterator

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, TraceIdRatioBased
from pbip_api.core.config import Settings
from pbip_api.core.logging import _opentelemetry_processor
from pbip_api.core.tracing import (
    _build_sampler,
    get_active_trace_context,
    get_in_memory_exporter,
    setup_tracing,
)
from pbip_api.main import create_app
from pbip_test_support import reset_tracing
from pydantic import SecretStr, ValidationError

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_tracing() -> Iterator[None]:
    reset_tracing()
    yield
    reset_tracing()


def _traced_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"otel_enabled": True, "otel_exporter": "memory"}
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]  # pydantic validates the values


def _deployed(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "app_env": "staging",
        "jwt_secret": SecretStr("s" * 48),
        "field_encryption_key": SecretStr(Fernet.generate_key().decode()),
        "cookie_secure": True,
        "cors_origins": "https://bank.example",
    }
    values.update(overrides)
    return values


def test_tracing_is_off_by_default_and_the_app_is_not_instrumented() -> None:
    settings = Settings()
    assert settings.otel_enabled is False
    assert settings.otel_exporter == "none"  # turning tracing on never defaults to memory
    assert setup_tracing(settings) is None

    app = create_app(settings)
    assert not getattr(app, "_is_instrumented_by_opentelemetry", False)
    with TestClient(app) as client:
        assert client.get("/api/health/live").status_code == 200
    assert get_in_memory_exporter() is None
    assert get_active_trace_context() == (None, None)


def test_the_memory_exporter_is_refused_when_deployed() -> None:
    with pytest.raises(ValidationError, match="OTEL_EXPORTER=memory"):
        Settings(**_deployed(otel_enabled=True, otel_exporter="memory"))  # type: ignore[arg-type]
    # The exporters meant for real use are accepted.
    for exporter in ("none", "console"):
        Settings(**_deployed(otel_enabled=True, otel_exporter=exporter))  # type: ignore[arg-type]


def test_setup_is_idempotent_and_never_overrides_the_global_provider(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _traced_settings()
    first = setup_tracing(settings)
    with caplog.at_level(logging.WARNING, logger="opentelemetry.trace"):
        second = setup_tracing(settings)
        create_app(settings)  # a second app in the same process reuses the provider
    assert first is not None and second is first
    assert trace.get_tracer_provider() is first
    assert "Overriding of current TracerProvider is not allowed" not in caplog.text


def test_resource_names_the_service_version_and_environment() -> None:
    provider = setup_tracing(_traced_settings(otel_service_name="pbip-api-test", app_env="test"))
    assert provider is not None
    attributes = provider.resource.attributes
    assert attributes["service.name"] == "pbip-api-test"
    assert attributes["deployment.environment"] == "test"
    assert attributes["service.version"]


def test_sampler_follows_the_sample_rate() -> None:
    assert _build_sampler(1.0) is ALWAYS_ON
    assert _build_sampler(0.0) is ALWAYS_OFF
    ratio = _build_sampler(0.1)
    assert isinstance(ratio, TraceIdRatioBased)
    assert ratio.rate == 0.1


def test_log_processor_adds_trace_ids_only_inside_a_span() -> None:
    assert "trace_id" not in _opentelemetry_processor(None, "info", {"event": "outside"})

    setup_tracing(_traced_settings())
    with trace.get_tracer("test").start_as_current_span("work") as span:
        context = span.get_span_context()
        processed = _opentelemetry_processor(None, "info", {"event": "inside"})
        assert get_active_trace_context() == (
            f"{context.trace_id:032x}",
            f"{context.span_id:016x}",
        )
    assert processed["trace_id"] == f"{context.trace_id:032x}"
    assert processed["span_id"] == f"{context.span_id:016x}"


def test_request_span_drops_the_query_string_and_correlates_the_access_log(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = create_app(_traced_settings())
    capsys.readouterr()
    with TestClient(app) as client:
        # 401 before any database access: the route needs a bearer token.
        response = client.get("/api/v1/accounts?email=alice@demo.pbip&token=abc123")
    assert response.status_code == 401

    exporter = get_in_memory_exporter()
    assert exporter is not None
    (server,) = [s for s in exporter.get_finished_spans() if s.kind.name == "SERVER"]
    values = " ".join(str(v) for v in (server.attributes or {}).values())
    assert "abc123" not in values
    assert "alice" not in values
    assert (server.attributes or {}).get("http.route") == "/api/v1/accounts"

    access = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{") and '"http_request"' in line
    ]
    assert access, "no access-log line captured"
    assert access[-1]["trace_id"] == f"{server.context.trace_id:032x}"


def test_health_and_metrics_are_not_traced() -> None:
    app = create_app(_traced_settings())
    with TestClient(app) as client:
        assert client.get("/api/health/live").status_code == 200
        client.get("/metrics")
    exporter = get_in_memory_exporter()
    assert exporter is not None
    assert exporter.get_finished_spans() == ()


def test_sample_rate_zero_records_no_spans_but_requests_still_work() -> None:
    app = create_app(_traced_settings(otel_sample_rate=0.0))
    with TestClient(app) as client:
        assert client.get("/api/v1/accounts").status_code == 401
    exporter = get_in_memory_exporter()
    assert exporter is not None
    assert exporter.get_finished_spans() == ()
