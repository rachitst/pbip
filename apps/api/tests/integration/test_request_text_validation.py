"""Regression (fuzzing run 40910803036): unstorable text in free-text fields was a 500, not a 422."""

import json
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pbip_test_support import PASSWORD, Actor

pytestmark = [pytest.mark.integration, pytest.mark.security]


def _post_escaped_json(
    client: TestClient, path: str, payload: dict[str, str], headers: dict[str, str]
) -> Response:
    """Send JSON with non-ASCII escaped (``\\ud800``), as clients on the wire do; a lone surrogate
    cannot be UTF-8 encoded by the test client itself, which is exactly what broke the server."""
    body = json.dumps(payload).encode("ascii")
    return client.post(path, headers={**headers, "Content-Type": "application/json"}, content=body)


def test_payee_name_with_nul_or_lone_surrogate_is_rejected_at_the_boundary(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    for name in ("Bob\x00Builder", "Bob\ud800"):
        resp = _post_escaped_json(
            client,
            "/api/v1/payees",
            {"name": name, "account_number": "PBIP123456", "bank_code": "PBIP0001"},
            alice.headers,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "validation_error"


def test_registration_full_name_with_control_characters_is_rejected(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": "nul@example.com", "password": PASSWORD, "full_name": "A\nB\x00C"},
    )
    assert resp.status_code == 422, resp.text


def test_lone_surrogate_password_and_fingerprint_are_rejected_at_the_boundary(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    """Regression (audit H8): a lone surrogate reached Argon2 / .encode() and produced a 500."""
    resp = _post_escaped_json(
        client,
        "/api/v1/auth/login",
        {"email": "a@example.com", "password": PASSWORD + "\ud800"},
        {},
    )
    assert resp.status_code == 422, resp.text
    alice = make_customer()
    resp = _post_escaped_json(
        client,
        "/api/v1/customers/devices",
        {"fingerprint": "\ud800", "label": "phone"},
        alice.headers,
    )
    assert resp.status_code == 422, resp.text


def test_fraud_decision_note_with_nul_is_rejected(
    client: TestClient, make_staff: Callable[..., Actor]
) -> None:
    from uuid import uuid4

    from pbip_api.modules.auth.models import RoleName

    analyst = make_staff(RoleName.FRAUD_ANALYST)
    resp = _post_escaped_json(
        client,
        f"/api/v1/fraud/alerts/{uuid4()}/decision",
        {"action": "RELEASE", "note": "ok\x00"},
        analyst.headers,
    )
    assert resp.status_code == 422, resp.text  # validation runs before the 404 lookup
