"""Held products as a recommendation input (TASK-111, ADR-0030).

Verifies, against real PostgreSQL and the real accounts, loans and wealth services:
- the engine is given the caller's open accounts, active loans and open fixed deposits, and only
  the caller's (a client-supplied customer id changes nothing), with no ids in the input;
- a held product is suppressed (a savings account; a deposit below the top-up line);
- an allowed top-up (a further fixed deposit) carries a reason that says it is a top-up and why.
"""

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.db import utcnow
from pbip_api.modules.loans.catalogue import PRODUCTS, LoanType
from pbip_api.modules.recommendations import service as recommendations_service
from pbip_ml.recommendations import (
    CustomerRecommendationContext,
    HeldProduct,
    HeldProductKind,
    Recommendation,
    generate_recommendations,
)
from pbip_ml.recommendations.engine import (
    DEPOSIT_TOP_UP_BUFFER_MONTHS,
    FIXED_DEPOSIT_ID,
    PRODUCT_CATALOGUE,
    SAVINGS_ACCOUNT_ID,
)
from pbip_ml.recommendations.propensity import PropensityModel
from pbip_test_support import Actor, age_payee, post_deposit_at
from sqlalchemy import Engine
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration, pytest.mark.security]

RECS = "/api/v1/recommendations"
FD_REGULAR = "20000000-0000-4000-a000-000000000002"  # 7.10%, 12-36 months
PERSONAL = next(p for p in PRODUCTS if p.loan_type == LoanType.PERSONAL)
SAVINGS = HeldProduct(kind=HeldProductKind.SAVINGS_ACCOUNT)


@pytest.fixture
def engine_inputs(monkeypatch: pytest.MonkeyPatch) -> list[CustomerRecommendationContext]:
    """Record exactly what the recommendation engine is given, then run it for real."""
    seen: list[CustomerRecommendationContext] = []
    real = recommendations_service.generate_recommendations

    def spy(
        ctx: CustomerRecommendationContext,
        top_k: int = 3,
        propensity: PropensityModel | None = None,
    ) -> list[Recommendation]:
        seen.append(ctx)
        return real(ctx, top_k=top_k, propensity=propensity)

    monkeypatch.setattr(recommendations_service, "generate_recommendations", spy)
    return seen


def _book_deposit(client: TestClient, actor: Actor, amount: int) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/wealth/fixed-deposits/book",
        headers={**actor.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "product_id": FD_REGULAR,
            "amount_minor": amount,
            "tenure_months": 12,
            "account_id": str(actor.account_id),
        },
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _take_loan(client: TestClient, actor: Actor, amount: int) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/loans/apply",
        headers={**actor.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "product_id": str(PERSONAL.id),
            "amount_minor": amount,
            "tenure_months": 12,
            "account_id": str(actor.account_id),
        },
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _spend(
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
            "reference": "Rent",
        },
    )
    assert resp.status_code == 201, resp.text


def _paid_this_month_and_next(engine: Engine, actor: Actor, salary: int, month: datetime) -> None:
    """A salary credited now and on the 1st of ``month`` (next month, when the engine is asked):
    recurring income then (ADR-0043) without adding an earlier, spend-free month to the buffer."""
    post_deposit_at(engine, actor.account_id, salary, "Salary", utcnow())
    post_deposit_at(engine, actor.account_id, salary, "Salary", month.replace(day=1, hour=0))


def _next_month() -> datetime:
    """Mid next month: this month's spending is then a closed month, which the buffer uses."""
    now = utcnow()
    return (now.replace(day=1) + timedelta(days=32)).replace(day=15)


def test_a_held_savings_account_is_not_recommended_again(
    client: TestClient,
    make_customer: Callable[..., Actor],
    engine_inputs: list[CustomerRecommendationContext],
) -> None:
    alice = make_customer(funds=200_000_00)  # registration opens a savings account

    body = client.get(RECS, headers=alice.headers).json()

    (ctx,) = engine_inputs
    assert ctx.held_products == (SAVINGS,)
    assert str(SAVINGS_ACCOUNT_ID) not in {item["product_id"] for item in body["recommendations"]}
    # the same customer without the holding would have been offered it: the holding suppressed it
    without = generate_recommendations(ctx.model_copy(update={"held_products": ()}), top_k=6)
    assert SAVINGS_ACCOUNT_ID in {rec.product_id for rec in without}


def test_a_held_deposit_is_suppressed_below_the_top_up_line_and_offered_above_it(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
    engine_inputs: list[CustomerRecommendationContext],
    db: Session,
) -> None:
    bob = make_customer()
    month = _next_month()
    # 4 lakh opening deposit and a 3 lakh salary this month and next: 10 lakh paid in each.
    # Carol spends 2 lakh this month and keeps ~7 lakh: about 3.5 months of buffer, enough for a
    # deposit (2 months) but below the 6-month top-up line once she holds one.
    carol = make_customer(funds=4_00_000_00)
    _paid_this_month_and_next(migrated_engine, carol, 3_00_000_00, month)
    _spend(client, carol, bob, make_verified_payee, migrated_engine, 2_00_000_00)
    # Dave spends 50,000 and keeps ~8.5 lakh: about 17 months, above the top-up line.
    dave = make_customer(funds=4_00_000_00)
    _paid_this_month_and_next(migrated_engine, dave, 3_00_000_00, month)
    _spend(client, dave, bob, make_verified_payee, migrated_engine, 50_000_00)

    assert carol.customer_id is not None and dave.customer_id is not None
    every = len(PRODUCT_CATALOGUE)
    before = recommendations_service.get_customer_recommendations(
        db, carol.customer_id, month, every
    )
    assert FIXED_DEPOSIT_ID in {r.product_id for r in before.recommendations}  # eligible, not held

    _book_deposit(client, carol, 1_00_000_00)
    _book_deposit(client, dave, 1_00_000_00)
    engine_inputs.clear()

    carol_recs = recommendations_service.get_customer_recommendations(
        db, carol.customer_id, month, every
    )
    dave_recs = recommendations_service.get_customer_recommendations(
        db, dave.customer_id, month, every
    )
    carol_ctx, dave_ctx = engine_inputs
    deposit = HeldProduct(kind=HeldProductKind.FIXED_DEPOSIT, amount_minor=1_00_000_00)
    assert carol_ctx.held_products == dave_ctx.held_products == (SAVINGS, deposit)

    assert 2.0 <= carol_ctx.buffer_months < DEPOSIT_TOP_UP_BUFFER_MONTHS
    assert FIXED_DEPOSIT_ID not in {r.product_id for r in carol_recs.recommendations}

    assert dave_ctx.buffer_months >= DEPOSIT_TOP_UP_BUFFER_MONTHS
    (top_up,) = [r for r in dave_recs.recommendations if r.product_id == FIXED_DEPOSIT_ID]
    assert top_up.reasons[0] == (
        "You already hold 1 fixed deposit (INR 1,00,000.00); this is a top-up because your "
        f"balance still covers {dave_ctx.buffer_months:.1f} months of spending, above the 6 "
        "months we look for before suggesting another deposit."
    )
    assert len(top_up.reasons) >= 2  # the deposit's own reasons still follow


def test_inputs_are_read_only_for_the_callers_own_customer(
    client: TestClient,
    make_customer: Callable[..., Actor],
    engine_inputs: list[CustomerRecommendationContext],
) -> None:
    alice = make_customer(salary=5_00_000_00)  # recurring income for the loan; 10 lakh balance
    bob = make_customer(funds=50_000_00)
    deposit = _book_deposit(client, alice, 1_00_000_00)
    loan = _take_loan(client, alice, 1_00_000_00)

    mine = client.get(RECS, headers=alice.headers).json()
    # Bob asks for Alice's recommendations by id: the parameter is not part of the route
    probe = client.get(f"{RECS}?customer_id={alice.customer_id}", headers=bob.headers).json()

    alice_ctx, bob_ctx = engine_inputs
    assert alice_ctx.customer_id == alice.customer_id
    assert {h.kind for h in alice_ctx.held_products} == {
        HeldProductKind.SAVINGS_ACCOUNT,
        HeldProductKind.FIXED_DEPOSIT,
        HeldProductKind.PERSONAL_LOAN,
    }
    loan_held = next(h for h in alice_ctx.held_products if h.kind == HeldProductKind.PERSONAL_LOAN)
    assert loan_held.amount_minor == loan["outstanding_principal_minor"] == 1_00_000_00
    # Bob's input holds only Bob's own savings account, never Alice's deposit or loan
    assert bob_ctx.customer_id == bob.customer_id
    assert bob_ctx.held_products == (SAVINGS,)
    assert probe["customer_id"] == str(bob.customer_id) != mine["customer_id"]
    # the engine input carries kinds and amounts only: no account, deposit or loan ids
    dumped = str(alice_ctx.model_dump()["held_products"])
    for secret in (deposit["id"], loan["id"], str(alice.account_id), alice.account_number):
        assert str(secret) not in dumped
