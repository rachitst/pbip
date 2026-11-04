"""UC-07 risk tolerance in the recommendation engine (ADR-0056).

The customer's RTQ-1 band is a suitability rule applied with the eligibility rules: a product
riskier than the band is not offered, an aggressive band can be offered it, and without a band the
engine behaves as before and says so in the reasons. The served simulated-uptake model still only
re-orders what the rules allow.
"""

from uuid import UUID, uuid4

import pytest
from pbip_ml.recommendations.contract import (
    CustomerRecommendationContext,
    ProductType,
    RiskLevel,
)
from pbip_ml.recommendations.engine import (
    INDEX_SIP_ID,
    NO_RISK_BAND_REASON,
    PRODUCT_CATALOGUE,
    generate_recommendations,
    risk_band_rule,
)
from pbip_ml.recommendations.propensity import (
    FEATURE_NAMES,
    MODEL_VERSION,
    ProductModel,
    PropensityModel,
)

pytestmark = pytest.mark.unit

SIP = next(p for p in PRODUCT_CATALOGUE if p.product_id == INDEX_SIP_ID)


def _investor(band: RiskLevel | None) -> CustomerRecommendationContext:
    """A customer every rule allows the index-fund SIP for (buffer, balance, savings, health)."""
    return CustomerRecommendationContext(
        customer_id=uuid4(),
        segment="HIGH_NET_WORTH",
        health_score=88,
        monthly_inflow_minor=250_000_00,
        monthly_spend_minor=90_000_00,
        savings_ratio=0.64,
        discretionary_ratio=0.25,
        buffer_months=8.5,
        emi_ratio=0.05,
        total_balance_minor=750_000_00,
        risk_band=band,
    )


def _sip_model() -> PropensityModel:
    """A hand-set model that puts the SIP near 100% for everyone."""
    n = len(FEATURE_NAMES)
    return PropensityModel(
        MODEL_VERSION,
        {
            p.product_id: ProductModel(
                8.0 if p.product_id == INDEX_SIP_ID else -4.6, (0.0,) * n, (0.0,) * n, (1.0,) * n
            )
            for p in PRODUCT_CATALOGUE
        },
    )


def _ids(ctx: CustomerRecommendationContext, model: PropensityModel | None = None) -> set[UUID]:
    return {r.product_id for r in generate_recommendations(ctx, top_k=6, propensity=model)}


def test_only_the_investment_product_carries_a_risk_level_and_it_is_the_catalogue_high() -> None:
    """MF-INDEX is HIGH in the wealth catalogue (migration 0018), the level the assistant uses."""
    assert {p.product_id: p.risk_level for p in PRODUCT_CATALOGUE if p.risk_level} == {
        INDEX_SIP_ID: RiskLevel.HIGH
    }
    assert all(
        p.risk_level is not None
        for p in PRODUCT_CATALOGUE
        if p.product_type == ProductType.INVESTMENT
    )


@pytest.mark.parametrize("band", [RiskLevel.LOW, RiskLevel.MODERATE])
def test_a_band_below_the_products_risk_excludes_it(band: RiskLevel) -> None:
    assert risk_band_rule(SIP, _investor(band)) == (False, None)
    assert INDEX_SIP_ID not in _ids(_investor(band))


def test_a_high_band_is_offered_the_high_risk_product_with_the_band_in_the_last_reason() -> None:
    recs = generate_recommendations(_investor(RiskLevel.HIGH), top_k=6)
    sip = next(r for r in recs if r.product_id == INDEX_SIP_ID)

    assert sip.reasons[-1] == (
        "Your risk profile from the questionnaire is high; this is a high-risk product, so it "
        "fits that profile."
    )
    assert "INR 7,50,000.00" in sip.reasons[0]  # the eligibility figures still come first


def test_without_a_band_the_offer_is_unchanged_and_the_reason_says_so() -> None:
    without = generate_recommendations(_investor(None), top_k=6)
    high = generate_recommendations(_investor(RiskLevel.HIGH), top_k=6)
    sip = next(r for r in without if r.product_id == INDEX_SIP_ID)

    assert sip.reasons[-1] == NO_RISK_BAND_REASON
    assert [r.product_id for r in without] == [r.product_id for r in high]  # never a weight
    assert [r.score for r in without] == [r.score for r in high]


@pytest.mark.parametrize("band", [None, RiskLevel.LOW, RiskLevel.MODERATE, RiskLevel.HIGH])
def test_the_band_changes_no_other_products_offer_score_or_reasons(
    band: RiskLevel | None,
) -> None:
    base = {
        r.product_id: (r.score, r.reasons)
        for r in generate_recommendations(_investor(None), top_k=6)
        if r.product_id != INDEX_SIP_ID
    }
    other = {
        r.product_id: (r.score, r.reasons)
        for r in generate_recommendations(_investor(band), top_k=6)
        if r.product_id != INDEX_SIP_ID
    }
    assert other == base


def test_the_band_never_lets_an_ineligible_product_in() -> None:
    """Eligibility first: an aggressive band does not lift the SIP's buffer or balance floor."""
    poor = _investor(RiskLevel.HIGH).model_copy(
        update={"buffer_months": 1.0, "total_balance_minor": 20_000_00}
    )
    assert INDEX_SIP_ID not in _ids(poor)


@pytest.mark.parametrize("band", [RiskLevel.LOW, RiskLevel.MODERATE])
def test_the_served_model_cannot_bring_back_a_product_the_band_excludes(band: RiskLevel) -> None:
    ctx = _investor(band)
    assert _ids(ctx, _sip_model()) == _ids(ctx)
    assert INDEX_SIP_ID not in _ids(ctx, _sip_model())


def test_under_the_model_a_high_band_keeps_the_same_offers_and_reasons() -> None:
    ctx = _investor(RiskLevel.HIGH)
    rules = generate_recommendations(ctx, top_k=6)
    ranked = generate_recommendations(ctx, top_k=6, propensity=_sip_model())

    assert ranked[0].product_id == INDEX_SIP_ID
    assert {r.product_id: r.reasons for r in ranked} == {r.product_id: r.reasons for r in rules}
