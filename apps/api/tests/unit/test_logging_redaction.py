import json

import pytest
from pbip_api.core.logging import REDACTED, configure_logging, redact

pytestmark = pytest.mark.unit


def test_sensitive_keys_are_redacted_at_any_depth() -> None:
    event = {"user": {"password": "hunter2", "profile": {"api_key": "abc"}}, "note": "ok"}
    cleaned = redact(event)
    assert cleaned["user"]["password"] == REDACTED
    assert cleaned["user"]["profile"]["api_key"] == REDACTED
    assert cleaned["note"] == "ok"


@pytest.mark.parametrize(
    ("raw", "forbidden"),
    [
        ("login for jane.doe@example.com failed", "jane.doe@example.com"),
        ("transfer to 123456789012 done", "123456789012"),
        ("auth Bearer abcdefghijklmnop123", "abcdefghijklmnop123"),
        (
            "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlMTIz",
            "eyJhbGciOiJIUzI1NiJ9",
        ),
    ],
)
def test_sensitive_patterns_in_free_text_are_scrubbed(raw: str, forbidden: str) -> None:
    assert forbidden not in redact(raw)


def test_ordinary_keys_that_contain_pan_are_not_over_redacted() -> None:
    assert redact({"company": "Acme", "span": 3})["company"] == "Acme"


@pytest.mark.parametrize(
    "key", ["full_name", "date_of_birth", "phone", "phone_masked", "customer_phone"]
)
def test_customer_pii_keys_are_redacted_at_any_depth(key: str) -> None:
    """PII that is never meant to be logged is redacted by key as a backstop (#145)."""
    event = {"customer": {key: "Alice Demo", "nested": {key: "1990-01-01"}}, "segment": "retail"}
    cleaned = redact(event)
    assert cleaned["customer"][key] == REDACTED
    assert cleaned["customer"]["nested"][key] == REDACTED
    assert cleaned["segment"] == "retail"


def test_identifier_keys_are_not_over_redacted_by_the_pii_parts() -> None:
    event = {"username": "svc-a", "route_name": "/api/v1/accounts", "customer_id": "c-1"}
    assert redact(event) == event


def test_end_to_end_log_line_never_contains_customer_pii(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import structlog

    configure_logging("INFO")
    structlog.get_logger("test").info(
        "profile_viewed",
        full_name="Alice Demo",
        date_of_birth="1990-01-01",
        phone="+91 98765 43210",
    )
    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["full_name"] == payload["date_of_birth"] == payload["phone"] == REDACTED
    for leaked in ("Alice Demo", "1990-01-01", "98765"):
        assert leaked not in line


def test_end_to_end_log_line_never_contains_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    import structlog

    configure_logging("INFO")
    structlog.get_logger("test").info("login_attempt", password="hunter2", email="a@b.example")
    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["password"] == REDACTED
    assert "a@b.example" not in line
    assert "hunter2" not in line
