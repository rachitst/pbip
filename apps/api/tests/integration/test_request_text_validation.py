"""Regression (fuzzing run 40910803036): unstorable text in free-text fields was a 500, not a 422."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from pbip_test_support import PASSWORD, Actor

pytestmark = [pytest.mark.integration, pytest.mark.security]


def test_payee_name_with_nul_or_lone_surrogate_is_rejected_at_the_boundary(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    for name in ("Bob\x00Builder", "Bob\ud800"):
        resp = client.post(
            "/api/v1/payees",
            headers=alice.headers,
            json={"name": name, "account_number": "PBIP123456", "bank_code": "PBIP0001"},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "validation_error"


def test_registration_full_name_with_control_characters_is_rejected() -> None:
    from pbip_test_support import build_app

    with TestClient(build_app()) as client:
        resp = client.post(
            "/api/v1/auth/register",
            json={"email": "nul@example.com", "password": PASSWORD, "full_name": "A\nB\x00C"},
        )
    assert resp.status_code == 422, resp.text
