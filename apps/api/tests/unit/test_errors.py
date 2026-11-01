import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.core.errors import NotFoundError, register_error_handlers
from pydantic import BaseModel

pytestmark = pytest.mark.unit


class Payload(BaseModel):
    password: str
    amount: int


def make_client() -> TestClient:
    app = FastAPI()
    register_error_handlers(app)

    @app.post("/echo")
    def echo(payload: Payload) -> dict[str, int]:
        return {"amount": payload.amount}

    @app.get("/missing")
    def missing() -> None:
        raise NotFoundError("account not found")

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("db password is hunter2")

    return TestClient(app, raise_server_exceptions=False)


def test_domain_error_becomes_problem_json() -> None:
    response = make_client().get("/missing")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "not_found"


def test_validation_error_never_echoes_submitted_values() -> None:
    response = make_client().post("/echo", json={"password": "hunter2", "amount": "not-a-number"})
    assert response.status_code == 422
    assert "hunter2" not in response.text
    assert "not-a-number" not in response.text


def test_unhandled_exception_returns_generic_500_without_details() -> None:
    response = make_client().get("/boom")
    assert response.status_code == 500
    assert "hunter2" not in response.text
    assert response.json()["code"] == "internal_error"


def test_unhandled_exception_is_tracked_with_route_and_location_but_no_data() -> None:
    """TASK-103 error tracking: searchable event + metric, request id, route template, never the message."""
    from pbip_api.core.metrics import UNHANDLED_ERRORS_TOTAL
    from structlog.testing import capture_logs

    before = UNHANDLED_ERRORS_TOTAL.labels("/boom", "RuntimeError")._value.get()
    with capture_logs() as logs:
        response = make_client().get("/boom")
    assert response.status_code == 500
    [event] = [e for e in logs if e["event"] == "unhandled_exception"]
    assert event["log_level"] == "error"
    assert event["route"] == "/boom" and event["method"] == "GET"
    assert event["error_type"] == "RuntimeError"
    assert event["raised_at"].startswith("test_errors.py:") and event["raised_at"].endswith(":boom")
    assert "hunter2" not in repr(event)  # the exception message never reaches the log
    assert UNHANDLED_ERRORS_TOTAL.labels("/boom", "RuntimeError")._value.get() == before + 1
