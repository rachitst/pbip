"""Integration tests for Relationship Manager behavioral insights API (TASK-056).

Verifies:
- GET /api/v1/insights/customers returns customers ordered by churn risk, one page at a time
- Calculates churn signals based on balance decline rate and inactivity days
- Strict RBAC: Protected by require_roles([RELATIONSHIP_MANAGER, ADMIN], mfa=True)
- Customers, unauthenticated requests, and non-RM staff are rejected
- Segment and churn risk score filtering and pagination work properly
"""

import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.modules.analytics import service as analytics_service
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.insights.schemas import ChurnRiskLevel
from pbip_api.modules.insights.service import (
    calculate_churn_signals,
    compute_balance_decline_rate,
    compute_inactivity_days,
)
from pbip_test_support import Actor, age_payee
from sqlalchemy import Engine, event, text
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


def _move_money(
    client: TestClient,
    payer: Actor,
    payee_owner: Actor,
    make_verified_payee: Callable[..., dict[str, Any]],
    engine: Engine,
    amount: int,
) -> None:
    payee = make_verified_payee(payer, payee_owner)
    age_payee(engine, payee["id"], 48)
    resp = client.post(
        "/api/v1/transfers",
        headers={**payer.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "from_account_id": str(payer.account_id),
            "amount_minor": amount,
            "payee_id": payee["id"],
            "reference": "Monthly rent",
        },
    )
    assert resp.status_code == 201, resp.text


def test_churn_signals_are_exact_on_known_ledger_data(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
    db: Session,
) -> None:
    """Deposit 1,00,000 then pay out 40,000: a window starting between them shows a 40% decline."""
    alice = make_customer(funds=100_000_00)
    bob = make_customer()
    _move_money(client, alice, bob, make_verified_payee, migrated_engine, 40_000_00)

    times = (
        db.execute(
            text(
                "SELECT e.created_at FROM transaction_entries e "
                "WHERE e.account_id = :account ORDER BY e.created_at"
            ),
            {"account": alice.account_id},
        )
        .scalars()
        .all()
    )
    deposited, paid = times[0], times[-1]
    assert paid > deposited
    as_of = paid + timedelta(days=10)
    window = as_of - (deposited + (paid - deposited) / 2)  # the window starts between the two

    activity = {
        a.customer_id: a for a in analytics_service.get_customer_activity(db, as_of, window)
    }
    mine = activity[alice.customer_id]
    assert (mine.balance_window_start_minor, mine.balance_minor) == (100_000_00, 60_000_00)
    assert mine.last_activity_at == paid  # the payment is the latest activity (login was earlier)
    decline = compute_balance_decline_rate(mine.balance_window_start_minor, mine.balance_minor)
    inactive = compute_inactivity_days(mine.last_activity_at, mine.created_at, as_of)
    assert (decline, inactive) == (0.4, 10)
    assert calculate_churn_signals(inactive, decline) == (0.295, ChurnRiskLevel.LOW)
    # Bob received the money: no decline
    theirs = activity[bob.customer_id]
    assert (theirs.balance_window_start_minor, theirs.balance_minor) == (0, 40_000_00)


def test_rows_carry_only_what_an_rm_needs(
    client: TestClient, make_customer: Callable[..., Actor], make_staff: Callable[..., Actor]
) -> None:
    make_customer(funds=150_000_00)
    rm = make_staff(RoleName.RELATIONSHIP_MANAGER, mfa=True)
    body = client.get("/api/v1/insights/customers", headers=rm.headers).json()
    assert body["items"]
    assert set(body["items"][0]) == {
        "customer_id",
        "full_name",
        "segment",
        "total_balance_minor",
        "inactivity_days",
        "balance_decline_rate",
        "churn_risk_score",
        "churn_risk_level",
        "top_recommended_product",
        "top_product_score",
        "high_value",
    }  # no email, phone, address, date of birth or account number


def test_cost_does_not_grow_with_customers_outside_the_page(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    migrated_engine: Engine,
) -> None:
    rm = make_staff(RoleName.RELATIONSHIP_MANAGER, mfa=True)

    def statements_for_one_row() -> int:
        count = 0

        def counter(*_args: object) -> None:
            nonlocal count
            count += 1

        event.listen(migrated_engine, "before_cursor_execute", counter)
        try:
            resp = client.get("/api/v1/insights/customers?limit=1", headers=rm.headers)
        finally:
            event.remove(migrated_engine, "before_cursor_execute", counter)
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 1
        return count

    for _ in range(2):
        make_customer(funds=10_000_00)
    few = statements_for_one_row()
    for _ in range(4):
        make_customer(funds=10_000_00)
    many = statements_for_one_row()
    assert many == few


def test_pagination_and_filters(
    client: TestClient, make_customer: Callable[..., Actor], make_staff: Callable[..., Actor]
) -> None:
    for _ in range(3):
        make_customer(funds=10_000_00)
    rm = make_staff(RoleName.RELATIONSHIP_MANAGER, mfa=True)
    first = client.get("/api/v1/insights/customers?limit=2", headers=rm.headers).json()
    second = client.get("/api/v1/insights/customers?limit=2&offset=2", headers=rm.headers).json()
    assert first["total"] == second["total"] == 3
    ids = [i["customer_id"] for i in first["items"] + second["items"]]
    assert len(ids) == len(set(ids)) == 3
    unsegmented = client.get(
        "/api/v1/insights/customers?segment=UNSEGMENTED", headers=rm.headers
    ).json()
    assert unsegmented["total"] == 3
    risky = client.get("/api/v1/insights/customers?min_churn_risk=0.9", headers=rm.headers).json()
    assert risky["total"] == 0  # all just active
    too_big = client.get("/api/v1/insights/customers?limit=26", headers=rm.headers)
    assert too_big.status_code == 422
