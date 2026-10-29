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
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.db import utcnow
from pbip_api.modules.accounts.models import Account
from pbip_api.modules.analytics import repository
from pbip_api.modules.analytics.models import MonthlyCategorySpending
from pbip_api.modules.analytics.service import materialize_monthly_spending
from pbip_api.modules.ledger import service as ledger
from pbip_test_support import Actor, age_payee
from sqlalchemy import Engine, select
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
    migrated_engine: Engine,
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


def _transfer(client: TestClient, actor: Actor, payee_id: str, amount: int, reference: str) -> UUID:
    resp = client.post(
        "/api/v1/transfers",
        headers={**actor.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "from_account_id": str(actor.account_id),
            "amount_minor": amount,
            "payee_id": payee_id,
            "reference": reference,
        },
    )
    assert resp.status_code == 201, resp.text
    return UUID(resp.json()["id"])


def _stored_rows(db: Session, account_id: UUID | None) -> set[tuple[str, int, int]]:
    db.expire_all()
    rows = db.execute(
        select(MonthlyCategorySpending).where(MonthlyCategorySpending.account_id == account_id)
    ).scalars()
    return {(r.category, r.total_amount_minor, r.transaction_count) for r in rows}


def test_reversed_transfers_and_refund_outflows_are_not_spending(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
    db: Session,
) -> None:
    """A reversed transfer was refunded, and the counterparty's REVERSAL debit returns money."""
    alice = make_customer(funds=500_000)
    bob = make_customer()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)
    _transfer(client, alice, payee["id"], 20_000, "BigBasket weekly groceries")
    dinner = _transfer(client, alice, payee["id"], 10_000, "Swiggy dinner order")
    ledger.reverse_transaction(db, dinner, None, "customer dispute")
    db.commit()

    alice_view = client.get(
        f"/api/v1/analytics/spending?account_id={alice.account_id}", headers=alice.headers
    ).json()
    assert alice_view["total_spend_minor"] == 20_000 and alice_view["transaction_count"] == 1
    assert [c["category"] for c in alice_view["categories"]] == ["groceries"]

    bob_view = client.get(
        f"/api/v1/analytics/spending?account_id={bob.account_id}", headers=bob.headers
    ).json()
    assert bob_view["total_spend_minor"] == 0 and bob_view["categories"] == []


def test_materialization_matches_the_live_computation_and_is_idempotent(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
    db: Session,
) -> None:
    """The nightly task stores exactly what the live computation returns and can be re-run."""
    alice = make_customer(funds=500_000)
    bob = make_customer()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)
    _transfer(client, alice, payee["id"], 20_000, "BigBasket weekly groceries")
    _transfer(client, alice, payee["id"], 10_000, "Zepto instant provisions")
    dinner = _transfer(client, alice, payee["id"], 10_000, "Swiggy dinner order")
    now = utcnow()

    # Only Alice's account has customer debits (the funding deposit debits the SYSTEM clearing
    # account, Bob only receives credits).
    debited = repository.accounts_with_debits(db, now.year, now.month)
    details = db.execute(
        select(Account.id, Account.account_number, Account.account_type).where(
            Account.id.in_(debited)
        )
    ).all()
    assert debited == [alice.account_id], details
    assert materialize_monthly_spending(db, periods=[(now.year, now.month)]) == 1
    assert _stored_rows(db, alice.account_id) == {("groceries", 30_000, 2), ("dining", 10_000, 1)}
    assert _stored_rows(db, bob.account_id) == set()

    # Re-running after a reversal updates groceries in place and deletes the vanished category.
    ledger.reverse_transaction(db, dinner, None, "customer dispute")
    db.commit()
    assert materialize_monthly_spending(db, periods=[(now.year, now.month)]) == 1
    assert _stored_rows(db, alice.account_id) == {("groceries", 30_000, 2)}
    assert materialize_monthly_spending(db, periods=[(now.year, now.month)]) == 1  # no-op rerun
    assert _stored_rows(db, alice.account_id) == {("groceries", 30_000, 2)}


def test_stored_rows_serve_closed_months_only_and_percentages_need_a_baseline(
    client: TestClient,
    make_customer: Callable[..., Actor],
    db: Session,
) -> None:
    """Rows for the running month are ignored by reads; a month without a predecessor has no %."""
    alice = make_customer(funds=100_000)
    now = utcnow()
    db.add(
        MonthlyCategorySpending(
            account_id=alice.account_id,
            year=now.year,
            month=now.month,
            category="groceries",
            total_amount_minor=99_000,
            transaction_count=9,
        )
    )
    db.add(
        MonthlyCategorySpending(
            account_id=alice.account_id,
            year=2025,
            month=6,
            category="groceries",
            total_amount_minor=60_000,
            transaction_count=3,
        )
    )
    db.commit()

    running = client.get(
        f"/api/v1/analytics/spending?account_id={alice.account_id}", headers=alice.headers
    ).json()
    assert running["total_spend_minor"] == 0  # live computation, not the stale stored row

    trends = client.get(
        f"/api/v1/analytics/trends?account_id={alice.account_id}&year=2025&month=6",
        headers=alice.headers,
    ).json()
    assert trends["current_month"]["total_spend_minor"] == 60_000
    assert trends["current_month"]["daily_burn_rate_minor"] == 60_000 // 30
    assert trends["previous_month"] is None
    assert trends["mom_spend_change_percentage"] is None
    assert trends["burn_rate_change_percentage"] is None
    assert trends["top_categories_increasing"][0]["change_percentage"] is None
    assert trends["top_categories_decreasing"] == []


def test_year_beyond_the_calendar_is_rejected_not_a_server_error(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    resp = client.get(
        f"/api/v1/analytics/trends?account_id={alice.account_id}&year=99999&month=1",
        headers=alice.headers,
    )
    assert resp.status_code == 422, resp.text
