import pytest
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.core.db import get_engine
from pbip_api.modules.health.router import ReadinessCache
from pbip_test_support import FakeConnection, build_app
from prometheus_client import REGISTRY
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def test_live_is_ok(client: TestClient) -> None:
    response = client.get("/api/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_ok_when_dependencies_are_up(client: TestClient) -> None:
    response = client.get("/api/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}


@pytest.mark.parametrize(
    ("db_ok", "redis_ok", "expected"),
    [
        (False, True, {"database": "fail", "redis": "ok"}),
        (True, False, {"database": "ok", "redis": "fail"}),
    ],
)
def test_ready_is_503_when_a_dependency_is_down(
    db_ok: bool, redis_ok: bool, expected: dict[str, str]
) -> None:
    with TestClient(build_app(db_ok=db_ok, redis_ok=redis_ok)) as test_client:
        response = test_client.get("/api/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "checks": expected}


def test_request_id_is_generated_and_a_valid_one_is_echoed(client: TestClient) -> None:
    generated = client.get("/api/health/live").headers["x-request-id"]
    assert len(generated) >= 8
    echoed = client.get("/api/health/live", headers={"X-Request-ID": "trace-abc-12345"})
    assert echoed.headers["x-request-id"] == "trace-abc-12345"


def test_malicious_request_id_is_replaced(client: TestClient) -> None:
    response = client.get("/api/health/live", headers={"X-Request-ID": "bad id\twith spaces"})
    assert response.headers["x-request-id"] != "bad id\twith spaces"


def test_metrics_use_route_templates_not_raw_paths(client: TestClient) -> None:
    client.get("/api/health/live")
    client.get("/definitely/not/a/route/12345")
    live = REGISTRY.get_sample_value(
        "http_requests_total", {"method": "GET", "route": "/api/health/live", "status": "200"}
    )
    unmatched = REGISTRY.get_sample_value(
        "http_requests_total", {"method": "GET", "route": "unmatched", "status": "404"}
    )
    assert live is not None
    assert unmatched is not None
    assert (
        REGISTRY.get_sample_value(
            "http_requests_total",
            {"method": "GET", "route": "/definitely/not/a/route/12345", "status": "404"},
        )
        is None
    )


def test_metrics_endpoint_exposes_prometheus_format(client: TestClient) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "http_request_duration_seconds" in response.text


class _CountingEngine:
    def __init__(self) -> None:
        self.connects = 0
        self.healthy = True

    def connect(self) -> object:
        self.connects += 1
        if not self.healthy:
            raise RuntimeError("db down")
        return FakeConnection()


def test_readiness_is_cached_for_the_ttl_then_rechecked() -> None:
    now = [100.0]
    engine = _CountingEngine()
    app = build_app()
    app.dependency_overrides[get_engine] = lambda: engine
    app.state.readiness_cache = ReadinessCache(1.0, clock=lambda: now[0])
    with TestClient(app) as test_client:
        for _ in range(5):
            assert test_client.get("/api/health/ready").status_code == 200
        assert engine.connects == 1  # one DB round trip for five probes within the TTL

        engine.healthy = False
        assert test_client.get("/api/health/ready").status_code == 200  # cached, <= 1 s stale
        now[0] += 1.0
        degraded = test_client.get("/api/health/ready")
        assert degraded.status_code == 503 and degraded.json()["checks"]["database"] == "fail"
        assert engine.connects == 2


def test_readiness_cache_can_be_disabled() -> None:
    engine = _CountingEngine()
    app = build_app(health_ready_cache_seconds=0)
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app) as test_client:
        for _ in range(3):
            test_client.get("/api/health/ready")
    assert engine.connects == 3


def test_readiness_cache_ttl_is_bounded() -> None:
    with pytest.raises(ValidationError):
        Settings(app_env="local", health_ready_cache_seconds=-1)
    with pytest.raises(ValidationError):
        Settings(app_env="local", health_ready_cache_seconds=31)
