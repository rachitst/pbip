"""TASK-045/046: loan catalogue, EMI schedule and explainable eligibility against PostgreSQL."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.modules.auth.models import RoleName, SecurityEvent, SecurityEventType
from pbip_api.modules.loans.catalogue import PRODUCTS, LoanType
from pbip_test_support import Actor, age_payee
from sqlalchemy import Engine
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

PERSONAL = next(p for p in PRODUCTS if p.loan_type == LoanType.PERSONAL)


def _check(client: TestClient, actor: Actor, amount: int, months: int = 36) -> Any:
    return client.post(
        "/api/v1/loans/check-eligibility",
        headers=actor.headers,
        json={"product_id": str(PERSONAL.id), "amount_minor": amount, "tenure_months": months},
    )


def test_catalogue_and_emi_schedule(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    products = client.get("/api/v1/loans/products", headers=alice.headers).json()
    assert {p["loan_type"] for p in products} == {"PERSONAL", "AUTO", "HOME"}
    resp = client.post(
        "/api/v1/loans/calculate-emi",
        headers=alice.headers,
        json={"product_id": str(PERSONAL.id), "principal_minor": 1_00_000_00, "tenure_months": 12},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["schedule"]) == 12
    assert sum(r["principal_minor"] for r in body["schedule"]) == 1_00_000_00
    assert body["total_payable_minor"] == 1_00_000_00 + body["total_interest_minor"]
    bad = client.post(
        "/api/v1/loans/calculate-emi",
        headers=alice.headers,
        json={"product_id": str(PERSONAL.id), "principal_minor": 1_00_000_00, "tenure_months": 13},
    )
    assert bad.status_code == 422  # tenure not offered


def test_eligible_with_explained_figures(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer(funds=1_00_000_00)  # INR 1,00,000 of income this month
    body = _check(client, alice, 5_00_000_00).json()
    assert body["status"] == "ELIGIBLE", body
    assert body["monthly_income_minor"] == 1_00_000_00 and body["existing_emi_minor"] == 0
    assert body["dti_with_loan"] <= 0.5
    assert body["max_eligible_amount_minor"] >= 5_00_000_00
    assert body["tenor_options"] and "within the 50% limit" in body["reasons"][0]


def test_rejected_when_dti_would_exceed_half_of_income(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer(funds=50_000_00)  # INR 50,000 income
    body = _check(client, alice, 15_00_000_00, 12).json()  # EMI ~ INR 1.3 lakh
    assert body["status"] == "NOT_ELIGIBLE"
    assert body["dti_with_loan"] > 0.5
    assert "above the 50% limit" in body["reasons"][0]
    assert body["max_eligible_amount_minor"] < 15_00_000_00


def test_rejected_when_existing_repayments_already_exceed_half(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> None:
    alice, lender = make_customer(funds=1_00_000_00), make_customer()
    payee = make_verified_payee(alice, lender)
    age_payee(migrated_engine, payee["id"], 48)
    resp = client.post(
        "/api/v1/transfers",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": 60_000_00,
            "payee_id": payee["id"],
            "reference": "Home loan EMI",
        },
    )
    assert resp.status_code in (201, 202), resp.text
    body = _check(client, alice, 1_00_000_00).json()
    assert body["status"] == "NOT_ELIGIBLE"
    assert body["dti_existing"] > 0.5
    assert "already take" in body["reasons"][0]
    assert body["tenor_options"] == []  # no budget left


def test_rejected_after_a_recent_lockout(
    client: TestClient, make_customer: Callable[..., Actor], db: Session
) -> None:
    alice = make_customer(funds=1_00_000_00)
    db.add(SecurityEvent(user_id=alice.user_id, event_type=SecurityEventType.ACCOUNT_LOCKED))
    db.commit()
    body = _check(client, alice, 1_00_000_00).json()
    assert body["status"] == "NOT_ELIGIBLE"
    assert "locked" in body["reasons"][0]
    assert body["max_eligible_amount_minor"] == 0


def test_loans_are_customer_only(client: TestClient, make_staff: Callable[..., Actor]) -> None:
    assert client.get("/api/v1/loans/products").status_code == 401
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    assert client.get("/api/v1/loans/products", headers=analyst.headers).status_code == 403
