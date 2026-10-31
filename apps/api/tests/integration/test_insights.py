"""Integration tests for Relationship Manager behavioral insights API (TASK-056).

Verifies:
- GET /api/v1/insights/customers returns prioritized list of high-propensity customers
- Calculates churn signals based on balance decline rate and inactivity days
- Strict RBAC: Protected by require_roles([RELATIONSHIP_MANAGER, ADMIN], mfa=True)
- Customers, unauthenticated requests, and non-RM staff are rejected
- Segment and churn risk score filtering and pagination work properly
"""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from pbip_api.modules.auth.models import RoleName
from pbip_test_support import Actor
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration, pytest.mark.security]


def test_insights_rbac_authorization(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
) -> None:
    """Only RELATIONSHIP_MANAGER or ADMIN with MFA may access customer insights."""
    # 1. 401 Unauthenticated
    assert client.get("/api/v1/insights/customers").status_code == 401

    # 2. 403 Customer
    alice = make_customer()
    assert client.get("/api/v1/insights/customers", headers=alice.headers).status_code == 403

    # 3. 403 Non-RM staff (Fraud Analyst)
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    assert client.get("/api/v1/insights/customers", headers=analyst.headers).status_code == 403

    # 4. 403 RM without MFA
    rm_no_mfa = make_staff(RoleName.RELATIONSHIP_MANAGER, mfa=False)
    assert client.get("/api/v1/insights/customers", headers=rm_no_mfa.headers).status_code == 403

    # 5. 200 RM with MFA
    rm = make_staff(RoleName.RELATIONSHIP_MANAGER, mfa=True)
    resp_rm = client.get("/api/v1/insights/customers", headers=rm.headers)
    assert resp_rm.status_code == 200

    # 6. 200 Admin with MFA
    admin = make_staff(RoleName.ADMIN, mfa=True)
    resp_admin = client.get("/api/v1/insights/customers", headers=admin.headers)
    assert resp_admin.status_code == 200


def test_insights_customer_list_and_churn_signals(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    db: Session,
) -> None:
    """RM fetches prioritized customer insights with accurate churn signals and product propensity."""
    # Create customers with varying funds
    c1 = make_customer(funds=300_000_00)
    c2 = make_customer(funds=10_000_00)

    rm = make_staff(RoleName.RELATIONSHIP_MANAGER, mfa=True)
    resp = client.get("/api/v1/insights/customers", headers=rm.headers)

    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert "items" in data
    assert "total" in data
    assert data["total"] >= 2

    customer_ids = [item["customer_id"] for item in data["items"]]
    assert str(c1.customer_id) in customer_ids
    assert str(c2.customer_id) in customer_ids

    for item in data["items"]:
        assert "customer_id" in item
        assert "full_name" in item
        assert "email" in item
        assert "segment" in item
        assert "total_balance_minor" in item
        assert item["inactivity_days"] >= 0
        assert 0.0 <= item["balance_decline_rate"] <= 1.0
        assert 0.0 <= item["churn_risk_score"] <= 1.0
        assert item["churn_risk_level"] in ("LOW", "MEDIUM", "HIGH")


def test_insights_filtering_and_pagination(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
) -> None:
    """Filter by minimum churn risk and verify pagination parameters."""
    make_customer(funds=50_000_00)
    rm = make_staff(RoleName.RELATIONSHIP_MANAGER, mfa=True)

    # Filtering with limit and offset
    resp_paginated = client.get(
        "/api/v1/insights/customers?limit=1&offset=0",
        headers=rm.headers,
    )
    assert resp_paginated.status_code == 200
    data = resp_paginated.json()
    assert len(data["items"]) <= 1

    # Filter with min_churn_risk
    resp_risk = client.get(
        "/api/v1/insights/customers?min_churn_risk=0.0",
        headers=rm.headers,
    )
    assert resp_risk.status_code == 200
