"""UC-06 catalogue loans in the recommendation engine (ADR-0056).

The loans module assesses every catalogue loan; the engine offers only the eligible ones the
customer does not hold, in place of the abstract credit line, with the credit line's ordering
score, the loans module's own reasons and at most one loan among the top places.
"""

from typing import Literal
from uuid import UUID, uuid4

import pytest
from pbip_ml.recommendations.contract import (
    CustomerRecommendationContext,
    HeldProduct,
    HeldProductKind,
    LoanOffer,
    ProductType,
)
from pbip_ml.recommendations.engine import (
    CREDIT_LINE_ID,
    LOAN_CTA_LABEL,
    PRODUCT_CATALOGUE,
    generate_recommendations,
    relevance_score,
)
from pbip_ml.recommendations.propensity import (
    FEATURE_NAMES,
    MODEL_VERSION,
    ProductModel,
    PropensityModel,
)

pytestmark = pytest.mark.unit

PERSONAL = UUID("10000000-0000-4000-a000-000000000001")
CAR = UUID("10000000-0000-4000-a000-000000000002")
HOME = UUID("10000000-0000-4000-a000-000000000003")
CREDIT_LINE = next(p for p in PRODUCT_CATALOGUE if p.product_id == CREDIT_LINE_ID)
REASONS = (
    "An EMI of INR 20,000.00 brings repayments to 20% of your income of INR 1,00,000.00, "
    "within the 50% limit.",
    "Your spending of INR 30,000.00 a month plus this EMI comes to 50% of your income, within "
    "the 90% limit.",
    "Your balance of INR 2,00,000.00 already covers the first EMI of INR 20,000.00.",
)


LoanType = Literal["PERSONAL", "AUTO", "HOME"]


def _offer(product_id: UUID, loan_type: LoanType, eligible: bool = True) -> LoanOffer:
    return LoanOffer(
        product_id=product_id,
        loan_type=loan_type,
        title=f"{loan_type.title()} loan",
        description="You could borrow INR 25,000.00 to INR 6,00,000.00 over 36 months.",
        eligible=eligible,
        min_amount_minor=25_000_00 if eligible else 0,
        max_amount_minor=6_00_000_00 if eligible else 0,
        tenure_months=36,
        emi_minor=20_000_00 if eligible else 0,
        annual_rate_bps=1150,
        reasons=REASONS if eligible else ("Your balance of INR 0.00 does not cover one EMI.",),
    )


def _ctx(*offers: LoanOffer, held: tuple[HeldProduct, ...] = ()) -> CustomerRecommendationContext:
    """A frequent transactor every credit-line rule would allow."""
    return CustomerRecommendationContext(
        customer_id=uuid4(),
        segment="FREQUENT_TRANSACTOR",
        health_score=60,
        monthly_inflow_minor=1_00_000_00,
        monthly_spend_minor=30_000_00,
        savings_ratio=0.30,
        discretionary_ratio=0.10,
        buffer_months=1.0,
        emi_ratio=0.0,
        total_balance_minor=2_00_000_00,
        held_products=held,
        loan_offers=offers,
    )


def _ids(ctx: CustomerRecommendationContext, top_k: int = 10) -> list[UUID]:
    return [r.product_id for r in generate_recommendations(ctx, top_k=top_k)]


def test_without_loan_offers_the_credit_line_is_offered_as_before() -> None:
    assert CREDIT_LINE_ID in _ids(_ctx())


def test_loan_offers_replace_the_credit_line_with_the_eligible_catalogue_loans() -> None:
    ids = _ids(_ctx(_offer(PERSONAL, "PERSONAL"), _offer(CAR, "AUTO", eligible=False)))
    assert CREDIT_LINE_ID not in ids
    assert PERSONAL in ids
    assert CAR not in ids  # the loans module said not eligible; the engine never re-decides


def test_no_eligible_loan_means_no_loan_and_no_credit_line() -> None:
    ids = _ids(_ctx(_offer(PERSONAL, "PERSONAL", eligible=False)))
    assert PERSONAL not in ids and CREDIT_LINE_ID not in ids


def test_a_loan_carries_its_assessment_reasons_kind_and_the_credit_lines_score() -> None:
    ctx = _ctx(_offer(PERSONAL, "PERSONAL"))
    loan = next(r for r in generate_recommendations(ctx, top_k=10) if r.product_id == PERSONAL)

    assert loan.product_type == ProductType.LOAN
    assert loan.reasons == list(REASONS)  # income, spending and balance, from the loans module
    assert loan.loan is not None and loan.loan.max_amount_minor == 6_00_000_00
    assert loan.cta_label == LOAN_CTA_LABEL
    assert loan.score == relevance_score(CREDIT_LINE, ctx)


def test_a_held_loan_type_is_not_offered_again() -> None:
    held = (HeldProduct(kind=HeldProductKind.PERSONAL_LOAN, amount_minor=1_00_000_00),)
    ids = _ids(_ctx(_offer(PERSONAL, "PERSONAL"), _offer(CAR, "AUTO"), held=held))
    assert PERSONAL not in ids
    assert CAR in ids


def test_only_the_first_eligible_loan_competes_and_the_rest_follow_every_other_product() -> None:
    ctx = _ctx(_offer(PERSONAL, "PERSONAL"), _offer(CAR, "AUTO"), _offer(HOME, "HOME"))
    recs = generate_recommendations(ctx, top_k=10)
    ids = [r.product_id for r in recs]

    assert ids[-2:] == [CAR, HOME]
    assert sum(1 for r in recs[:3] if r.loan is not None) == 1
    assert sum(1 for r in recs[: len(recs) - 2] if r.loan is not None) == 1


def test_only_loans_eligible_fill_the_top_places_with_loans() -> None:
    held = (HeldProduct(kind=HeldProductKind.SAVINGS_ACCOUNT),)  # not offered again
    loans = (_offer(PERSONAL, "PERSONAL"), _offer(CAR, "AUTO"), _offer(HOME, "HOME"))
    poor = _ctx(*loans, held=held).model_copy(
        update={"monthly_inflow_minor": 10_000_00, "health_score": 10, "savings_ratio": -0.5}
    )
    assert _ids(poor, top_k=3) == [PERSONAL, CAR, HOME]


def test_the_served_model_orders_a_loan_by_its_credit_line_probability_only() -> None:
    n = len(FEATURE_NAMES)
    model = PropensityModel(
        MODEL_VERSION,
        {
            p.product_id: ProductModel(
                8.0 if p.product_id == CREDIT_LINE_ID else -4.6, (0.0,) * n, (0.0,) * n, (1.0,) * n
            )
            for p in PRODUCT_CATALOGUE
        },
    )
    ctx = _ctx(_offer(PERSONAL, "PERSONAL"), _offer(CAR, "AUTO", eligible=False))
    ranked = generate_recommendations(ctx, top_k=10, propensity=model)
    rules = generate_recommendations(ctx, top_k=10)

    assert ranked[0].product_id == PERSONAL
    assert {r.product_id: r.reasons for r in ranked} == {r.product_id: r.reasons for r in rules}
    assert CAR not in {r.product_id for r in ranked}  # the model cannot add an ineligible loan


def test_a_loan_offer_without_reasons_is_refused() -> None:
    with pytest.raises(ValueError, match="reasons"):
        LoanOffer.model_validate({**_offer(PERSONAL, "PERSONAL").model_dump(), "reasons": ()})
