"""UC-06 (ADR-0056) through the API, against PostgreSQL.

- eligibility rule 4 (spending) refuses a check and an application whose debt ratio alone passes;
- GET /api/v1/loans/offers assesses every catalogue loan on the caller's own figures;
- GET /api/v1/recommendations offers catalogue loans (not the credit line) with their assessment,
  never a loan type the customer holds, and only loans the loans module found eligible.
"""

import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.db import utcnow
from pbip_api.modules.loans.catalogue import PRODUCTS, LoanType
from pbip_api.modules.recommendations import service as recommendations_service
from pbip_ml.recommendations.engine import CREDIT_LINE_ID
from pbip_test_support import Actor, month_start, post_spend_at
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

pytestmark = [pytest.mark.integration, pytest.mark.security]

OFFERS = "/api/v1/loans/offers"
PERSONAL = next(p for p in PRODUCTS if p.loan_type == LoanType.PERSONAL)
CAR = next(p for p in PRODUCTS if p.loan_type == LoanType.AUTO)
LOAN_IDS = {str(p.id) for p in PRODUCTS}


@pytest.fixture(autouse=True)
def cold_cache() -> None:
    recommendations_service.clear_context_cache()


def _spent_last_month(engine: Engine, actor: Actor, amount: int) -> None:
    """Salary is paid last month and this month (2 months observed), so monthly spend = amount/2."""
    when = month_start(utcnow(), 1) + timedelta(days=5)
    post_spend_at(engine, actor.account_id, amount, "Rent and bills", when)


def _check(client: TestClient, actor: Actor) -> Any:
    resp = client.post(
        "/api/v1/loans/check-eligibility",
        headers=actor.headers,
        json={"product_id": str(PERSONAL.id), "amount_minor": 1_00_000_00, "tenure_months": 36},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _all_recommendations(engine: Engine, actor: Actor) -> list[Any]:
    assert actor.customer_id is not None
    with sessionmaker(bind=engine)() as session:
        found = recommendations_service.get_customer_recommendations(
            session, actor.customer_id, top_k=recommendations_service.EVERY_PRODUCT
        )
    return list(found.recommendations)


def test_spending_refuses_a_loan_the_debt_ratio_alone_allows_and_apply_follows(
    client: TestClient, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    alice = make_customer(salary=1_00_000_00)
    _spent_last_month(migrated_engine, alice, 1_78_000_00)  # INR 89,000 a month

    body = _check(client, alice)
    assert body["status"] == "NOT_ELIGIBLE", body
    assert body["dti_with_loan"] <= 0.50
    assert body["monthly_spend_minor"] == 89_000_00
    assert body["available_balance_minor"] == 22_000_00
    assert body["spending_with_loan_ratio"] > 0.90
    assert body["rules_version"] == "loan-eligibility-v2"
    assert "Your spending of INR 89,000.00 a month" in body["reasons"][0]
    assert "above the 90% limit" in body["reasons"][0]

    applied = client.post(
        "/api/v1/loans/apply",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "product_id": str(PERSONAL.id),
            "amount_minor": 1_00_000_00,
            "tenure_months": 36,
            "account_id": str(alice.account_id),
        },
    )
    assert applied.status_code == 422, applied.text
    assert "above the 90% limit" in applied.json()["title"]


def test_an_eligible_check_explains_income_spending_and_balance(
    client: TestClient, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    alice = make_customer(salary=1_00_000_00)
    _spent_last_month(migrated_engine, alice, 60_000_00)  # INR 30,000 a month

    body = _check(client, alice)
    assert body["status"] == "ELIGIBLE", body
    assert "of your income of INR 1,00,000.00, within the 50% limit" in body["reasons"][0]
    assert body["reasons"][1].startswith("Your spending of INR 30,000.00 a month plus this EMI")
    assert body["reasons"][2].startswith("Your balance of INR 1,40,000.00 already covers")


def test_offers_assess_every_catalogue_loan_with_a_suggested_range(
    client: TestClient, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    alice = make_customer(salary=1_00_000_00)
    _spent_last_month(migrated_engine, alice, 60_000_00)

    resp = client.get(OFFERS, headers=alice.headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["monthly_income_minor"], body["monthly_spend_minor"]) == (1_00_000_00, 30_000_00)
    assert body["available_balance_minor"] == 1_40_000_00
    assert [o["product_id"] for o in body["offers"]] == [str(p.id) for p in PRODUCTS]
    personal = body["offers"][0]
    assert personal["status"] == "ELIGIBLE" and personal["tenure_months"] == 36
    assert personal["suggested_min_amount_minor"] == PERSONAL.min_amount_minor
    assert personal["suggested_max_amount_minor"] > personal["suggested_min_amount_minor"]
    assert personal["emi_at_max_minor"] > 0
    # the maximum is itself eligible on /check-eligibility, the same rules
    check = client.post(
        "/api/v1/loans/check-eligibility",
        headers=alice.headers,
        json={
            "product_id": personal["product_id"],
            "amount_minor": personal["suggested_max_amount_minor"],
            "tenure_months": personal["tenure_months"],
        },
    )
    assert check.json()["status"] == "ELIGIBLE", check.text


def test_offers_without_income_are_not_eligible_and_say_why(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    newcomer = make_customer(funds=5_00_000_00)  # a one-off deposit is not income (ADR-0043)
    body = client.get(OFFERS, headers=newcomer.headers).json()
    assert {o["status"] for o in body["offers"]} == {"NOT_ELIGIBLE"}
    assert all(o["suggested_max_amount_minor"] == 0 for o in body["offers"])
    assert all("could not find regular income" in o["reasons"][0] for o in body["offers"])


def test_recommendations_offer_catalogue_loans_with_their_assessment_not_the_credit_line(
    client: TestClient, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    alice = make_customer(salary=1_00_000_00)
    _spent_last_month(migrated_engine, alice, 60_000_00)

    every = _all_recommendations(migrated_engine, alice)
    assert CREDIT_LINE_ID not in {r.product_id for r in every}
    loans = [r for r in every if str(r.product_id) in LOAN_IDS]
    assert loans and all(r.product_type == "LOAN" and r.loan is not None for r in loans)
    offers = {
        o["product_id"]: o for o in client.get(OFFERS, headers=alice.headers).json()["offers"]
    }
    for rec in loans:
        offer = offers[str(rec.product_id)]
        assert offer["status"] == "ELIGIBLE"
        assert rec.loan is not None
        assert rec.loan.suggested_max_amount_minor == offer["suggested_max_amount_minor"]
        assert rec.reasons == offer["reasons"][:3]
    assert sum(1 for r in every[: len(every) - len(loans) + 1] if r.loan is not None) == 1

    resp = client.get("/api/v1/recommendations", headers=alice.headers)
    assert resp.status_code == 200
    for item in resp.json()["recommendations"]:
        assert item["product_type"] in ("SAVINGS", "LOAN", "INVESTMENT", "INSURANCE")
        assert (item["loan"] is not None) == (item["product_id"] in LOAN_IDS)


def test_an_ineligible_customer_is_recommended_no_loan(
    client: TestClient, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    alice = make_customer(salary=1_00_000_00)
    _spent_last_month(migrated_engine, alice, 1_96_000_00)  # INR 98,000 a month: no room at all
    every = _all_recommendations(migrated_engine, alice)
    assert not any(r.product_type == "LOAN" and r.loan is not None for r in every)
    assert CREDIT_LINE_ID not in {r.product_id for r in every}


def test_a_held_loan_type_is_not_recommended_again(
    client: TestClient, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    alice = make_customer(salary=2_00_000_00)
    applied = client.post(
        "/api/v1/loans/apply",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "product_id": str(PERSONAL.id),
            "amount_minor": 1_00_000_00,
            "tenure_months": 12,
            "account_id": str(alice.account_id),
        },
    )
    assert applied.status_code == 201, applied.text
    ids = {r.product_id for r in _all_recommendations(migrated_engine, alice)}
    assert PERSONAL.id not in ids
    assert CAR.id in ids  # another loan type the rules still allow
