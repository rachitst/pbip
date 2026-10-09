import pytest
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.main import create_app
from pydantic import SecretStr

pytestmark = pytest.mark.security

REQUIRED = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "cache-control": "no-store",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
}


def test_api_responses_carry_security_headers(client: TestClient) -> None:
    headers = client.get("/api/health/live").headers
    for name, value in REQUIRED.items():
        assert headers.get(name) == value, name
    assert "strict-transport-security" not in headers  # only over TLS


def test_error_responses_also_carry_security_headers(client: TestClient) -> None:
    headers = client.get("/no/such/route").headers
    assert headers.get("x-content-type-options") == "nosniff"


def test_production_enables_hsts_and_disables_docs() -> None:
    settings = Settings(
        app_env="production",
        jwt_secret=SecretStr("y" * 48),
        field_encryption_key=SecretStr("k"),
        cookie_secure=True,
        cors_origins="https://bank.example",
    )
    with TestClient(create_app(settings)) as prod_client:
        assert "max-age" in prod_client.get("/api/health/live").headers["strict-transport-security"]
        assert prod_client.get("/docs").status_code == 404
        assert prod_client.get("/openapi.json").status_code == 404


def test_cors_rejects_unknown_origin(client: TestClient) -> None:
    response = client.get("/api/health/live", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers


def test_cors_allows_configured_origin(client: TestClient) -> None:
    response = client.get("/api/health/live", headers={"Origin": "http://localhost:5173"})
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"
