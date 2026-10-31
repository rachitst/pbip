"""Integration tests for customer personalized product recommendations API (TASK-054).

Verifies:
- GET /api/v1/recommendations returns top-3 eligible products ordered by the rule-based score
- Explanations and reasons are grounded and human-readable
- Authentication required (401 unauthenticated, 403 staff)
- Strict BOLA: Customer receives recommendations for their own profile only
"""

from collections.abc import Callable
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.customers.models import Customer
from pbip_test_support import Actor
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration, pytest.mark.security]


def test_recommendations_happy_path(
    client: TestClient,
    make_customer: Callable[..., Actor],
) -> None:
    """Customer receives top-3 personalized product recommendations with reasons."""
    alice = make_customer(funds=200_000_00)

    resp = client.get("/api/v1/recommendations", headers=alice.headers)

    assert resp.status_code == 200, resp.text

    data = resp.json()
    assert data["customer_id"] == str(alice.customer_id)
    assert data["model_version"] == "rules-v1"
    assert "evaluated_at" in data

    recs = data["recommendations"]
    assert 1 <= len(recs) <= 3

    for item in recs:
        assert "product_id" in item
        assert item["product_type"] in ("SAVINGS", "LOAN", "INVESTMENT", "INSURANCE")
        assert item["title"]
        assert item["description"]
        assert 0.0 <= item["score"] <= 1.0
        assert len(item["reasons"]) >= 1
        assert item["cta_label"]
        assert "cta_path" not in item  # no product pages exist yet


def test_recommendations_unauthenticated_and_staff_forbidden(
    client: TestClient,
    make_staff: Callable[..., Actor],
) -> None:
    """Unauthenticated returns 401; staff returns 403."""
    # 401
    resp_unauth = client.get("/api/v1/recommendations")
    assert resp_unauth.status_code == 401

    # 403
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    resp_staff = client.get("/api/v1/recommendations", headers=analyst.headers)
    assert resp_staff.status_code == 403


def test_recommendations_use_the_customers_real_figures_and_ignore_date_of_birth(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    """Reasons quote the real balance; a date of birth without consent changes nothing (ADR-0015)."""
    alice = make_customer(funds=200_000_00)
    before = client.get("/api/v1/recommendations", headers=alice.headers).json()
    reasons = [reason for item in before["recommendations"] for reason in item["reasons"]]
    assert reasons, before
    assert any("INR" in reason for reason in reasons)
    assert not any("%" in item["description"] for item in before["recommendations"])

    customer = db.get(Customer, alice.customer_id)
    assert customer is not None
    customer.date_of_birth = date(1950, 1, 1)  # 60+ but no consent mechanism exists yet
    db.commit()
    after = client.get("/api/v1/recommendations", headers=alice.headers).json()
    assert after["recommendations"] == before["recommendations"]


def test_recommendations_are_scoped_to_the_authenticated_customer(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer(funds=200_000_00)
    bob = make_customer()
    a = client.get("/api/v1/recommendations", headers=alice.headers).json()
    b = client.get("/api/v1/recommendations", headers=bob.headers).json()
    assert a["customer_id"] == str(alice.customer_id)
    assert b["customer_id"] == str(bob.customer_id)
    # a client-supplied customer id is not a parameter: it is ignored
    c = client.get(
        f"/api/v1/recommendations?customer_id={bob.customer_id}", headers=alice.headers
    ).json()
    assert c["customer_id"] == str(alice.customer_id)
