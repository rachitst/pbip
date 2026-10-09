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


def test_end_to_end_log_line_never_contains_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    import structlog

    configure_logging("INFO")
    structlog.get_logger("test").info("login_attempt", password="hunter2", email="a@b.example")
    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["password"] == REDACTED
    assert "a@b.example" not in line
    assert "hunter2" not in line
