"""Unit tests for personalized product recommendation engine (TASK-054).

Verifies eligibility exclusion rules, logistic propensity ranking, explainable reasons,
score stability, and ADR-0015 age band considerations.
"""

from uuid import uuid4

import pytest
from pbip_ml.recommendations.contract import (
    CustomerRecommendationContext,
    ProductType,
    Recommendation,
)
from pbip_ml.recommendations.engine import (
    CREDIT_LINE_ID,
    FIXED_DEPOSIT_ID,
    INDEX_SIP_ID,
    PRODUCT_CATALOGUE,
    REWARDS_CARD_ID,
    SAVINGS_ACCELERATOR_ID,
    evaluate_eligibility,
    generate_recommendations,
)

pytestmark = pytest.mark.unit


def test_eligibility_exclusion_for_zero_buffer_and_overdraft() -> None:
    """Customers without emergency buffer are excluded from wealth investments and fixed deposits."""
    ctx = CustomerRecommendationContext(
        customer_id=uuid4(),
        segment="BUDGET_CONSCIOUS",
        health_score=35,
        monthly_inflow_minor=30_000_00,
        monthly_spend_minor=32_000_00,
        savings_ratio=-0.05,
        discretionary_ratio=0.10,
        buffer_months=0.0,  # Zero buffer!
        emi_ratio=0.30,
        total_balance_minor=0,
    )

    recs = generate_recommendations(ctx, top_k=3)
    rec_ids = {r.product_id for r in recs}

    # Index SIP and Fixed Deposit require positive buffer & balance
    assert INDEX_SIP_ID not in rec_ids
    assert FIXED_DEPOSIT_ID not in rec_ids
    # Eligible products like Savings Accelerator should still be offered
    assert SAVINGS_ACCELERATOR_ID in rec_ids


def test_eligibility_exclusion_for_high_debt_ratio() -> None:
    """Customers with debt obligations exceeding 45% cannot take additional credit lines."""
    ctx = CustomerRecommendationContext(
        customer_id=uuid4(),
        segment="FREQUENT_TRANSACTOR",
        health_score=40,
        monthly_inflow_minor=50_000_00,
        monthly_spend_minor=45_000_00,
        savings_ratio=0.10,
        discretionary_ratio=0.15,
        buffer_months=1.5,
        emi_ratio=0.55,  # 55% EMI-to-income!
        total_balance_minor=20_000_00,
    )

    credit_product = next(p for p in PRODUCT_CATALOGUE if p.product_id == CREDIT_LINE_ID)
    assert not evaluate_eligibility(credit_product, ctx)

    recs = generate_recommendations(ctx, top_k=3)
    assert CREDIT_LINE_ID not in {r.product_id for r in recs}


def test_high_net_worth_propensity_ranks_investments_top() -> None:
    """High net worth customers with strong health and savings rank wealth & deposits highest."""
    ctx = CustomerRecommendationContext(
        customer_id=uuid4(),
        segment="HIGH_NET_WORTH",
        health_score=88,
        monthly_inflow_minor=250_000_00,  # 2,50,000 INR
        monthly_spend_minor=90_000_00,
        savings_ratio=0.64,
        discretionary_ratio=0.25,
        buffer_months=8.5,
        emi_ratio=0.05,
        total_balance_minor=750_000_00,
        age_band="36-50",
    )

    recs = generate_recommendations(ctx, top_k=3)
    assert len(recs) == 3

    top_product = recs[0]
    # Top product should be Investment SIP or Fixed Deposit
    assert top_product.product_type in (ProductType.INVESTMENT, ProductType.SAVINGS)
    assert top_product.score > 0.70
    assert any("compounding" in r.lower() or "wealth" in r.lower() for r in top_product.reasons)


def test_discretionary_spender_propensity_ranks_rewards_card_top() -> None:
    """Customers with high lifestyle spending (dining/shopping) rank rewards card highest."""
    ctx = CustomerRecommendationContext(
        customer_id=uuid4(),
        segment="DISCRETIONARY_SPENDER",
        health_score=60,
        monthly_inflow_minor=80_000_00,
        monthly_spend_minor=65_000_00,
        savings_ratio=0.18,
        discretionary_ratio=0.65,  # 65% discretionary!
        buffer_months=2.0,
        emi_ratio=0.15,
        total_balance_minor=120_000_00,
        age_band="26-35",
    )

    recs = generate_recommendations(ctx, top_k=3)
    assert len(recs) == 3

    top_rec = recs[0]
    assert top_rec.product_id == REWARDS_CARD_ID
    assert top_rec.score > 0.65
    assert any("cashback" in r.lower() for r in top_rec.reasons)


def test_adr_0015_senior_citizen_rate_boost_reason() -> None:
    """Customers in the 60+ age band receive senior citizen interest rate reasons."""
    ctx = CustomerRecommendationContext(
        customer_id=uuid4(),
        segment="BUDGET_CONSCIOUS",
        health_score=75,
        monthly_inflow_minor=60_000_00,
        monthly_spend_minor=35_000_00,
        savings_ratio=0.40,
        discretionary_ratio=0.10,
        buffer_months=6.0,
        emi_ratio=0.0,
        total_balance_minor=300_000_00,
        age_band="60+",  # Senior citizen consented per ADR-0015
    )

    recs = generate_recommendations(ctx, top_k=3)
    fd_rec = next((r for r in recs if r.product_id == FIXED_DEPOSIT_ID), None)
    assert fd_rec is not None
    assert any("Senior Citizen 8.0%" in r for r in fd_rec.reasons)


def test_unknown_age_band_still_receives_standard_recommendations() -> None:
    """Customers without age consent (unknown age band) receive standard recommendations cleanly."""
    ctx = CustomerRecommendationContext(
        customer_id=uuid4(),
        segment="BUDGET_CONSCIOUS",
        health_score=70,
        monthly_inflow_minor=50_000_00,
        monthly_spend_minor=30_000_00,
        savings_ratio=0.40,
        discretionary_ratio=0.10,
        buffer_months=4.0,
        emi_ratio=0.0,
        total_balance_minor=150_000_00,
        age_band="unknown",
    )

    recs = generate_recommendations(ctx, top_k=3)
    assert len(recs) == 3
    # All recommendations must satisfy the contract
    for r in recs:
        assert isinstance(r, Recommendation)
        assert len(r.reasons) >= 1
        assert 0.0 <= r.score <= 1.0


def test_ranking_determinism_and_score_stability() -> None:
    """Identical contexts produce identical scores, ordering, and reasons."""
    ctx = CustomerRecommendationContext(
        customer_id=uuid4(),
        segment="FREQUENT_TRANSACTOR",
        health_score=55,
        monthly_inflow_minor=45_000_00,
        monthly_spend_minor=40_000_00,
        savings_ratio=0.11,
        discretionary_ratio=0.35,
        buffer_months=1.8,
        emi_ratio=0.20,
        total_balance_minor=60_000_00,
    )

    recs1 = generate_recommendations(ctx, top_k=3)
    recs2 = generate_recommendations(ctx, top_k=3)

    assert len(recs1) == len(recs2)
    for r1, r2 in zip(recs1, recs2, strict=True):
        assert r1.product_id == r2.product_id
        assert r1.score == r2.score
        assert r1.reasons == r2.reasons
