"""Held products as a recommendation input (TASK-111, ADR-0030).

A product the customer already holds is not offered again unless a top-up rule allows it, and then
the first reason says it is a top-up and why. Without holdings the output is exactly as before.
"""

from typing import Any
from uuid import uuid4

import pytest
from pbip_ml.recommendations.contract import (
    CustomerRecommendationContext,
    HeldProduct,
    HeldProductKind,
)
from pbip_ml.recommendations.engine import (
    CREDIT_LINE_ID,
    DEPOSIT_TOP_UP_BUFFER_MONTHS,
    FAMILY_COVER_ID,
    FIXED_DEPOSIT_ID,
    PRODUCT_CATALOGUE,
    REWARDS_CARD_ID,
    SAVINGS_ACCOUNT_ID,
    generate_recommendations,
    held_product_rule,
)
from pydantic import ValidationError

pytestmark = pytest.mark.unit

ALL = len(PRODUCT_CATALOGUE)


def _ctx(
    buffer_months: float = 4.0, *held: HeldProduct, **overrides: Any
) -> CustomerRecommendationContext:
    """A customer eligible for every product in the catalogue (buffer permitting)."""
    fields: dict[str, Any] = {
        "customer_id": uuid4(),
        "segment": "BUDGET_CONSCIOUS",
        "health_score": 80,
        "monthly_inflow_minor": 150_000_00,
        "monthly_spend_minor": 80_000_00,
        "savings_ratio": 0.45,
        "discretionary_ratio": 0.30,
        "buffer_months": buffer_months,
        "emi_ratio": 0.10,
        "total_balance_minor": 400_000_00,
        "held_products": held,
    }
    fields.update(overrides)
    return CustomerRecommendationContext(**fields)


def _ids(ctx: CustomerRecommendationContext) -> list[Any]:
    return [rec.product_id for rec in generate_recommendations(ctx, top_k=ALL)]


SAVINGS = HeldProduct(kind=HeldProductKind.SAVINGS_ACCOUNT)
CURRENT = HeldProduct(kind=HeldProductKind.CURRENT_ACCOUNT)
DEPOSIT = HeldProduct(kind=HeldProductKind.FIXED_DEPOSIT, amount_minor=100_000_00)
PERSONAL_LOAN = HeldProduct(kind=HeldProductKind.PERSONAL_LOAN, amount_minor=180_000_00)


def test_without_holdings_every_eligible_product_is_offered_as_before() -> None:
    ctx = _ctx()
    assert set(_ids(ctx)) == {p.product_id for p in PRODUCT_CATALOGUE}  # all six qualify
    assert all(held_product_rule(p, ctx) == (True, None) for p in PRODUCT_CATALOGUE)


def test_a_held_savings_account_is_not_offered_again() -> None:
    ids = _ids(_ctx(4.0, SAVINGS))
    assert SAVINGS_ACCOUNT_ID not in ids
    assert len(ids) == ALL - 1  # nothing else is affected


def test_a_current_account_alone_does_not_count_as_a_savings_account() -> None:
    assert SAVINGS_ACCOUNT_ID in _ids(_ctx(4.0, CURRENT))


def test_an_active_personal_loan_suppresses_the_credit_line_with_no_top_up() -> None:
    ctx = _ctx(12.0, PERSONAL_LOAN)  # a large buffer does not make a second unsecured loan
    assert CREDIT_LINE_ID not in _ids(ctx)
    credit_line = next(p for p in PRODUCT_CATALOGUE if p.product_id == CREDIT_LINE_ID)
    assert held_product_rule(credit_line, ctx) == (False, None)


@pytest.mark.parametrize("kind", [HeldProductKind.AUTO_LOAN, HeldProductKind.HOME_LOAN])
def test_a_secured_loan_is_not_the_credit_line(kind: HeldProductKind) -> None:
    assert CREDIT_LINE_ID in _ids(_ctx(4.0, HeldProduct(kind=kind, amount_minor=1_000_000_00)))


def test_a_held_deposit_is_suppressed_while_the_buffer_is_below_the_top_up_line() -> None:
    ctx = _ctx(DEPOSIT_TOP_UP_BUFFER_MONTHS - 0.1, DEPOSIT)
    assert FIXED_DEPOSIT_ID not in _ids(ctx)
    assert FIXED_DEPOSIT_ID in _ids(_ctx(DEPOSIT_TOP_UP_BUFFER_MONTHS - 0.1))  # eligible otherwise


def test_a_deposit_top_up_is_offered_with_a_reason_that_says_so() -> None:
    second = HeldProduct(kind=HeldProductKind.FIXED_DEPOSIT, amount_minor=50_000_00)
    ctx = _ctx(DEPOSIT_TOP_UP_BUFFER_MONTHS, DEPOSIT, second)
    rec = next(
        r for r in generate_recommendations(ctx, top_k=ALL) if r.product_id == FIXED_DEPOSIT_ID
    )
    assert rec.reasons[0] == (
        "You already hold 2 fixed deposits (INR 1,50,000.00); this is a top-up because your balance "
        "still covers 6.0 months of spending, above the 6 months we look for before suggesting "
        "another deposit."
    )
    # the deposit's own reasons follow, unchanged
    plain = next(
        r
        for r in generate_recommendations(_ctx(DEPOSIT_TOP_UP_BUFFER_MONTHS), top_k=ALL)
        if r.product_id == FIXED_DEPOSIT_ID
    )
    assert rec.reasons[1:] == plain.reasons
    assert rec.score == plain.score  # holding a product never changes its ranking score


def test_a_top_up_keeps_the_senior_citizen_reason() -> None:
    ctx = _ctx(8.0, DEPOSIT, age_band="60+")
    rec = next(
        r for r in generate_recommendations(ctx, top_k=ALL) if r.product_id == FIXED_DEPOSIT_ID
    )
    assert rec.reasons[0].startswith(
        "You already hold 1 fixed deposit (INR 1,00,000.00); this is a top-up"
    )
    assert rec.reasons[-1] == "Senior-citizen deposit rates apply because you are 60 or over."


def test_a_top_up_still_needs_the_products_eligibility_rules() -> None:
    ctx = _ctx(9.0, DEPOSIT, total_balance_minor=40_000_00)  # below the 50,000 minimum balance
    assert FIXED_DEPOSIT_ID not in _ids(ctx)


def test_products_nobody_can_hold_are_never_suppressed() -> None:
    every_kind = tuple(HeldProduct(kind=kind, amount_minor=1_00) for kind in HeldProductKind)
    ids = _ids(_ctx(4.0, *every_kind))
    assert {REWARDS_CARD_ID, FAMILY_COVER_ID} <= set(ids)
    assert SAVINGS_ACCOUNT_ID not in ids and CREDIT_LINE_ID not in ids


def test_suppression_frees_the_top_three_for_the_next_products() -> None:
    top = generate_recommendations(_ctx(4.0), top_k=3)
    held = HeldProduct(kind=HeldProductKind.SAVINGS_ACCOUNT)
    after = generate_recommendations(_ctx(4.0, held), top_k=3)
    assert len(after) == 3
    assert SAVINGS_ACCOUNT_ID not in {r.product_id for r in after}
    if SAVINGS_ACCOUNT_ID not in {r.product_id for r in top}:
        assert [r.product_id for r in after] == [r.product_id for r in top]


def test_only_reasons_change_the_output_shape_stays_the_same() -> None:
    rec = generate_recommendations(_ctx(8.0, DEPOSIT), top_k=ALL)[0]
    assert set(rec.model_dump()) == {
        "product_id",
        "product_type",
        "score",
        "reasons",
        "model_version",
        "title",
        "description",
        "cta_label",
    }
    assert rec.model_version == "rules-v1"


def test_held_product_input_is_bounded() -> None:
    with pytest.raises(ValidationError):
        HeldProduct(kind=HeldProductKind.FIXED_DEPOSIT, amount_minor=-1)
    with pytest.raises(ValidationError):
        HeldProduct.model_validate({"kind": "CREDIT_CARD"})
