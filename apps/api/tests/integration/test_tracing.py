"""TASK-085 against PostgreSQL: the real app, the real engine path, SQL child spans.

Run with ``-s`` to see the sample trace tree (the task's "sample trace verified in test output").
"""

import json
import sys
import uuid
from collections.abc import Iterator

import pytest
from fakeredis import FakeRedis
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import ReadableSpan
from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import get_engine
from pbip_api.core.redis import get_redis
from pbip_api.core.tracing import get_in_memory_exporter, instrument_engine
from pbip_api.main import create_app
from pbip_api.modules.auth.dependencies import get_clock
from pbip_test_support import PASSWORD, FakeClock, login_json, reset_tracing
from sqlalchemy import Engine, create_engine, event

pytestmark = pytest.mark.integration


@pytest.fixture
def traced_engine(
    migrated_engine: Engine, test_database_url: str, settings: Settings
) -> Iterator[tuple[Engine, Settings, list[str]]]:
    """A separate engine on the migrated test database, instrumented the way get_engine does."""
    reset_tracing()
    traced = settings.model_copy(update={"otel_enabled": True, "otel_exporter": "memory"})
    engine = create_engine(test_database_url, pool_pre_ping=True)
    statements: list[str] = []

    @event.listens_for(engine, "after_cursor_execute")
    def _record(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
        statements.append(statement)

    yield engine, traced, statements
    reset_tracing()
    engine.dispose()


def _tree(spans: tuple[ReadableSpan, ...], server: ReadableSpan) -> str:
    def line(span: ReadableSpan, depth: int) -> str:
        duration = ((span.end_time or 0) - (span.start_time or 0)) / 1_000_000
        return f"{'    ' * depth}{span.kind.name:6} {span.name}  ({duration:.2f} ms)"

    children = [s for s in spans if s.parent and s.parent.span_id == server.context.span_id]
    lines = [f"trace_id={server.context.trace_id:032x}", line(server, 0)]
    lines += [line(child, 1) for child in children]
    return "\n".join(lines)


def test_request_and_its_sql_share_one_trace_without_values_or_rewritten_sql(
    traced_engine: tuple[Engine, Settings, list[str]],
    fake_redis: FakeRedis,
    clock: FakeClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine, settings, statements = traced_engine
    app = create_app(settings)
    instrument_engine(engine, settings)  # what get_engine() does when OTEL_ENABLED=true
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_redis] = lambda: fake_redis
    app.dependency_overrides[get_clock] = lambda: clock
    email = f"trace-{uuid.uuid4().hex[:8]}@example.com"

    with TestClient(app) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": PASSWORD, "full_name": "Tracy Trace"},
        )
        assert registered.status_code == 201, registered.text
        token = login_json(client, email)["access_token"]
        exporter = get_in_memory_exporter()
        assert exporter is not None
        exporter.clear()
        capsys.readouterr()
        response = client.get("/api/v1/accounts", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200

    spans = exporter.get_finished_spans()
    (server,) = [s for s in spans if s.kind.name == "SERVER"]
    assert server.name == "GET /api/v1/accounts"
    sql = [s for s in spans if s.kind.name == "CLIENT"]
    assert sql, "no SQL spans: the engine is not instrumented"
    for span in sql:
        assert span.context.trace_id == server.context.trace_id
        assert span.parent is not None
        assert (span.attributes or {}).get("db.system") == "postgresql"

    # Bind values never reach span data (the customer's email is a bound parameter).
    everything = " ".join(str(v) for s in spans for v in (s.attributes or {}).values())
    assert email not in everything
    # SQL is sent unchanged: no sqlcommenter traceparent comments.
    assert statements and not any("traceparent" in statement for statement in statements)

    access = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{") and '"http_request"' in line
    ]
    assert access[-1]["trace_id"] == f"{server.context.trace_id:032x}"

    with capsys.disabled():
        sys.stdout.write("\nSample trace (TASK-085):\n" + _tree(spans, server) + "\n")
