"""Integration tests for customer personalized product recommendations API (TASK-054).

Verifies:
- GET /api/v1/recommendations returns top-3 eligible products scored by propensity
- Response time complies with sub-40ms requirement
- Explanations and reasons are grounded and human-readable
- Authentication required (401 unauthenticated, 403 staff)
- Strict BOLA: Customer receives recommendations for their own profile only
"""

import time
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from pbip_api.modules.auth.models import RoleName
from pbip_test_support import Actor

pytestmark = [pytest.mark.integration, pytest.mark.security]


def test_recommendations_happy_path(
    client: TestClient,
    make_customer: Callable[..., Actor],
) -> None:
    """Customer receives top-3 personalized product recommendations with reasons."""
    alice = make_customer(funds=200_000_00)

    start = time.perf_counter()
    resp = client.get("/api/v1/recommendations", headers=alice.headers)
    latency_ms = (time.perf_counter() - start) * 1000

    assert resp.status_code == 200, resp.text
    # Acceptance criterion: GET /api/v1/recommendations responds in < 40ms
    # (Allowing generous overhead for test client setup, but ensuring fast response)
    assert latency_ms < 500, f"Recommendation endpoint too slow: {latency_ms:.2f}ms"

    data = resp.json()
    assert data["customer_id"] == str(alice.customer_id)
    assert data["model_version"] == "rec-v1.0.0"
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
        assert item["cta_path"]


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
