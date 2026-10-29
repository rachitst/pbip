"""Integration tests for Customer Spending Trends & Category Aggregations API (TASK-040).

Verifies:
- GET /api/v1/analytics/spending returns accurate spend breakdown across categories (Groceries, Dining, etc.)
- GET /api/v1/analytics/trends computes month-over-month percentage changes and daily burn rate
- Queries leverage pre-aggregated monthly tables to maintain high performance
- BOLA policy guarantees customer can only query their own accounts (404 for non-owned or non-existent)
- Unauthenticated requests return 401; invalid inputs return 422
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.modules.analytics.models import MonthlyCategorySpending
from pbip_test_support import Actor, age_payee
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration, pytest.mark.security]


def test_spending_breakdown_empty_history(
    client: TestClient,
    make_customer: Callable[..., Actor],
) -> None:
    """Empty transaction history returns 200 with 0 total spend and empty categories."""
    alice = make_customer(funds=100_000)
    resp = client.get(
        f"/api/v1/analytics/spending?account_id={alice.account_id}",
        headers=alice.headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["account_id"] == str(alice.account_id)
    assert data["currency"] == "INR"
    assert data["total_spend_minor"] == 0
    assert data["transaction_count"] == 0
    assert data["categories"] == []


def test_spending_breakdown_with_categorized_debits(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Any,
) -> None:
    """Accurately calculates spend amounts, transaction counts, and percentages by category."""
    alice = make_customer(funds=500_000)
    bob = make_customer()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)

    # 1. Debit for groceries (20,000 minor = 200 INR)
    r1 = client.post(
        "/api/v1/transfers",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": 20_000,
            "payee_id": payee["id"],
            "reference": "BigBasket weekly groceries",
        },
    )
    assert r1.status_code == 201

    # 2. Debit for dining (10,000 minor = 100 INR)
    r2 = client.post(
        "/api/v1/transfers",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": 10_000,
            "payee_id": payee["id"],
            "reference": "Swiggy dinner order",
        },
    )
    assert r2.status_code == 201

    # 3. Another debit for groceries (10,000 minor = 100 INR)
    r3 = client.post(
        "/api/v1/transfers",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": 10_000,
            "payee_id": payee["id"],
            "reference": "Zepto instant provisions",
        },
    )
    assert r3.status_code == 201

    resp = client.get(
        f"/api/v1/analytics/spending?account_id={alice.account_id}",
        headers=alice.headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["total_spend_minor"] == 40_000
    assert data["transaction_count"] == 3

    categories = {c["category"]: c for c in data["categories"]}
    assert "groceries" in categories
    assert categories["groceries"]["amount_minor"] == 30_000
    assert categories["groceries"]["transaction_count"] == 2
    assert categories["groceries"]["percentage"] == 75.0

    assert "dining" in categories
    assert categories["dining"]["amount_minor"] == 10_000
    assert categories["dining"]["transaction_count"] == 1
    assert categories["dining"]["percentage"] == 25.0


def test_spending_queries_leverage_preaggregated_tables(
    client: TestClient,
    make_customer: Callable[..., Actor],
    db: Session,
) -> None:
    """Pre-aggregated monthly table data is returned directly."""
    alice = make_customer(funds=100_000)

    # Insert pre-aggregated rows for 2025-06
    db.add(
        MonthlyCategorySpending(
            account_id=alice.account_id,
            year=2025,
            month=6,
            category="groceries",
            total_amount_minor=85_000,
            transaction_count=5,
        )
    )
    db.add(
        MonthlyCategorySpending(
            account_id=alice.account_id,
            year=2025,
            month=6,
            category="utilities",
            total_amount_minor=15_000,
            transaction_count=2,
        )
    )
    db.commit()

    resp = client.get(
        f"/api/v1/analytics/spending?account_id={alice.account_id}&year=2025&month=6",
        headers=alice.headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["year"] == 2025
    assert data["month"] == 6
    assert data["period"] == "2025-06"
    assert data["total_spend_minor"] == 100_000
    assert data["transaction_count"] == 7
    assert len(data["categories"]) == 2

    cat_map = {c["category"]: c for c in data["categories"]}
    assert cat_map["groceries"]["amount_minor"] == 85_000
    assert cat_map["groceries"]["percentage"] == 85.0
    assert cat_map["utilities"]["amount_minor"] == 15_000
    assert cat_map["utilities"]["percentage"] == 15.0


def test_spending_trends_and_mom_calculations(
    client: TestClient,
    make_customer: Callable[..., Actor],
    db: Session,
) -> None:
    """Month-over-month trend analysis computes burn rate and percentage changes."""
    alice = make_customer(funds=100_000)

    # Pre-populate previous month (2025-08)
    db.add(
        MonthlyCategorySpending(
            account_id=alice.account_id,
            year=2025,
            month=8,
            category="groceries",
            total_amount_minor=50_000,
            transaction_count=4,
        )
    )
    db.add(
        MonthlyCategorySpending(
            account_id=alice.account_id,
            year=2025,
            month=8,
            category="dining",
            total_amount_minor=30_000,
            transaction_count=3,
        )
    )

    # Pre-populate current month (2025-09) with higher groceries and lower dining
    db.add(
        MonthlyCategorySpending(
            account_id=alice.account_id,
            year=2025,
            month=9,
            category="groceries",
            total_amount_minor=80_000,
            transaction_count=6,
        )
    )
    db.add(
        MonthlyCategorySpending(
            account_id=alice.account_id,
            year=2025,
            month=9,
            category="dining",
            total_amount_minor=20_000,
            transaction_count=2,
        )
    )
    db.commit()

    resp = client.get(
        f"/api/v1/analytics/trends?account_id={alice.account_id}&year=2025&month=9",
        headers=alice.headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["account_id"] == str(alice.account_id)
    assert data["current_month"]["total_spend_minor"] == 100_000
    assert data["current_month"]["period"] == "2025-09"
    assert data["current_month"]["daily_burn_rate_minor"] == 100_000 // 30

    assert data["previous_month"] is not None
    assert data["previous_month"]["total_spend_minor"] == 80_000
    assert data["previous_month"]["period"] == "2025-08"

    # MoM change: (100000 - 80000) / 80000 = +25.0%
    assert data["mom_spend_change_percentage"] == 25.0

    # Top increasing: groceries (+30,000)
    inc = data["top_categories_increasing"]
    assert len(inc) >= 1
    assert inc[0]["category"] == "groceries"
    assert inc[0]["change_minor"] == 30_000

    # Top decreasing: dining (-10,000)
    dec = data["top_categories_decreasing"]
    assert len(dec) >= 1
    assert dec[0]["category"] == "dining"
    assert dec[0]["change_minor"] == -10_000


def test_analytics_bola_isolation(
    client: TestClient,
    make_customer: Callable[..., Actor],
) -> None:
    """BOLA protection: querying another customer's account returns 404."""
    alice = make_customer(funds=100_000)
    bob = make_customer(funds=100_000)

    # Bob attempts to inspect Alice's spending
    resp_spending = client.get(
        f"/api/v1/analytics/spending?account_id={alice.account_id}",
        headers=bob.headers,
    )
    assert resp_spending.status_code == 404, resp_spending.text

    # Bob attempts to inspect Alice's trends
    resp_trends = client.get(
        f"/api/v1/analytics/trends?account_id={alice.account_id}",
        headers=bob.headers,
    )
    assert resp_trends.status_code == 404, resp_trends.text

    # Non-existent account UUID returns 404
    non_existent = uuid.uuid4()
    resp_non_existent = client.get(
        f"/api/v1/analytics/spending?account_id={non_existent}",
        headers=bob.headers,
    )
    assert resp_non_existent.status_code == 404

    # Bob attempts to inspect Alice's health score
    resp_health = client.get(
        f"/api/v1/analytics/health-score?account_id={alice.account_id}",
        headers=bob.headers,
    )
    assert resp_health.status_code == 404


def test_financial_health_score_endpoint(
    client: TestClient,
    make_customer: Callable[..., Actor],
) -> None:
    """GET /api/v1/analytics/health-score calculates health components dynamically."""
    alice = make_customer(funds=200_000)

    resp = client.get(
        f"/api/v1/analytics/health-score?account_id={alice.account_id}",
        headers=alice.headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["account_id"] == str(alice.account_id)
    assert data["currency"] == "INR"
    assert 0 <= data["overall_score"] <= 100
    assert data["rating"] in ("EXCELLENT", "GOOD", "FAIR", "NEEDS_ATTENTION")

    comps = data["components"]
    assert comps["savings_rate"]["weight_percentage"] == 30.0
    assert comps["expense_volatility"]["weight_percentage"] == 25.0
    assert comps["emi_to_income"]["weight_percentage"] == 25.0
    assert comps["buffer_months"]["weight_percentage"] == 20.0
    assert len(data["recommendations"]) >= 1


def test_analytics_validation(
    client: TestClient,
    make_customer: Callable[..., Actor],
) -> None:
    """Invalid parameter shapes are rejected with 422."""
    alice = make_customer()

    # Invalid UUID
    resp_uuid = client.get(
        "/api/v1/analytics/spending?account_id=not-a-uuid",
        headers=alice.headers,
    )
    assert resp_uuid.status_code == 422

    # Invalid month > 12
    resp_month = client.get(
        f"/api/v1/analytics/spending?account_id={alice.account_id}&month=13",
        headers=alice.headers,
    )
    assert resp_month.status_code == 422

    # Unauthenticated
    resp_unauth = client.get(f"/api/v1/analytics/spending?account_id={alice.account_id}")
    assert resp_unauth.status_code == 401
