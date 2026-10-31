"""Product recommendation engine: eligibility rules, propensity scoring, and explainability.

Architecture (TASK-054):
1. Evaluates eligibility rules first (e.g. savings buffer for investments, income for loans).
2. Scores customer propensity using a calibrated logistic regression ranking model.
3. Generates human-readable, grounded justification reasons.
4. Returns top-3 recommendations, adhering to ADR-0015 (demographics/age-band).
"""

import math
from collections.abc import Callable
from uuid import UUID

from pbip_ml.recommendations.contract import (
    BankingProduct,
    CustomerRecommendationContext,
    ProductType,
    Recommendation,
)

MODEL_VERSION = "rec-v1.0.0"

# Fixed deterministic UUIDs for standard banking catalogue
SAVINGS_ACCELERATOR_ID = UUID("00000000-0000-4000-a000-000000000001")
FIXED_DEPOSIT_ID = UUID("00000000-0000-4000-a000-000000000002")
CREDIT_LINE_ID = UUID("00000000-0000-4000-a000-000000000003")
REWARDS_CARD_ID = UUID("00000000-0000-4000-a000-000000000004")
INDEX_SIP_ID = UUID("00000000-0000-4000-a000-000000000005")
FAMILY_SHIELD_ID = UUID("00000000-0000-4000-a000-000000000006")


PRODUCT_CATALOGUE: tuple[BankingProduct, ...] = (
    BankingProduct(
        product_id=SAVINGS_ACCELERATOR_ID,
        product_type=ProductType.SAVINGS,
        title="High-Yield Savings Accelerator",
        description=(
            "Earn 6.5% APY on idle deposits with automated round-ups and daily liquidity."
        ),
        min_income_minor=0,
        min_buffer_months=0.0,
        min_savings_ratio=-1.0,
        max_emi_ratio=1.0,
        min_balance_minor=0,
        min_health_score=0,
        min_discretionary_ratio=0.0,
        target_segments=["BUDGET_CONSCIOUS", "INACTIVE_SAVER"],
        cta_label="Open Account",
        cta_path="/accounts/open?type=SAVINGS",
    ),
    BankingProduct(
        product_id=FIXED_DEPOSIT_ID,
        product_type=ProductType.SAVINGS,
        title="Fixed Term Deposit",
        description=(
            "Guaranteed 7.5% p.a. returns (8.0% for Senior Citizens 60+) with flexible tenures."
        ),
        min_income_minor=0,
        min_buffer_months=2.0,
        min_savings_ratio=0.05,
        max_emi_ratio=0.60,
        min_balance_minor=50_000_00,  # 50,000 INR
        min_health_score=40,
        min_discretionary_ratio=0.0,
        target_segments=["HIGH_NET_WORTH", "BUDGET_CONSCIOUS"],
        cta_label="Book Deposit",
        cta_path="/deposits/new",
    ),
    BankingProduct(
        product_id=CREDIT_LINE_ID,
        product_type=ProductType.LOAN,
        title="Instant Personal Credit Line",
        description=(
            "Pre-approved credit line up to ₹5,00,000. Interest charged only on utilized funds."
        ),
        min_income_minor=25_000_00,  # 25,000 INR monthly inflow
        min_buffer_months=0.0,
        min_savings_ratio=-0.20,
        max_emi_ratio=0.45,  # Existing debt must not exceed 45% of income
        min_balance_minor=0,
        min_health_score=30,
        min_discretionary_ratio=0.0,
        target_segments=["FREQUENT_TRANSACTOR", "DISCRETIONARY_SPENDER"],
        cta_label="Apply for Loan",
        cta_path="/loans/apply?type=PERSONAL",
    ),
    BankingProduct(
        product_id=REWARDS_CARD_ID,
        product_type=ProductType.LOAN,
        title="Titanium Rewards Credit Card",
        description=(
            "3% unlimited cashback on dining and travel, airport lounge access, and zero forex fee."
        ),
        min_income_minor=35_000_00,  # 35,000 INR monthly inflow
        min_buffer_months=0.5,
        min_savings_ratio=0.0,
        max_emi_ratio=0.50,
        min_balance_minor=0,
        min_health_score=40,
        min_discretionary_ratio=0.20,  # Must spend at least 20% on discretionary
        target_segments=["DISCRETIONARY_SPENDER", "FREQUENT_TRANSACTOR", "HIGH_NET_WORTH"],
        cta_label="Get Card",
        cta_path="/cards/apply?tier=TITANIUM",
    ),
    BankingProduct(
        product_id=INDEX_SIP_ID,
        product_type=ProductType.INVESTMENT,
        title="Balanced Index Wealth SIP",
        description=(
            "Low-cost automated index fund investing in blue-chip equities with zero exit load."
        ),
        min_income_minor=30_000_00,
        min_buffer_months=3.0,  # Requires at least 3 months emergency buffer
        min_savings_ratio=0.15,
        max_emi_ratio=0.40,
        min_balance_minor=100_000_00,  # 100,000 INR
        min_health_score=55,
        min_discretionary_ratio=0.0,
        target_segments=["HIGH_NET_WORTH", "BUDGET_CONSCIOUS"],
        cta_label="Start SIP",
        cta_path="/investments/sip/start",
    ),
    BankingProduct(
        product_id=FAMILY_SHIELD_ID,
        product_type=ProductType.INSURANCE,
        title="Comprehensive Family Protection Shield",
        description=(
            "₹1 Crore term life protection paired with cashless family health cover and riders."
        ),
        min_income_minor=15_000_00,
        min_buffer_months=0.0,
        min_savings_ratio=-0.10,
        max_emi_ratio=0.70,
        min_balance_minor=0,
        min_health_score=0,
        min_discretionary_ratio=0.0,
        target_segments=["FREQUENT_TRANSACTOR", "BUDGET_CONSCIOUS", "DISCRETIONARY_SPENDER"],
        cta_label="View Coverage",
        cta_path="/insurance/quote",
    ),
)


def evaluate_eligibility(
    product: BankingProduct,
    ctx: CustomerRecommendationContext,
) -> bool:
    """Evaluate hard eligibility exclusion criteria. Returns True if eligible."""
    if ctx.monthly_inflow_minor < product.min_income_minor:
        return False
    if ctx.buffer_months < product.min_buffer_months:
        return False
    if ctx.savings_ratio < product.min_savings_ratio:
        return False
    if ctx.emi_ratio > product.max_emi_ratio:
        return False
    if ctx.total_balance_minor < product.min_balance_minor:
        return False
    if ctx.health_score < product.min_health_score:
        return False
    return ctx.discretionary_ratio >= product.min_discretionary_ratio


def _score_product_propensity(
    product: BankingProduct,
    ctx: CustomerRecommendationContext,
) -> float:
    """Calculate calibrated propensity probability score using logistic model."""
    # Normalized feature inputs [0.0, 1.0]
    savings_norm = min(max(ctx.savings_ratio, 0.0), 1.0)
    disc_norm = min(max(ctx.discretionary_ratio, 0.0), 1.0)
    buffer_norm = min(ctx.buffer_months / 12.0, 1.0)
    income_norm = min(ctx.monthly_inflow_minor / 300_000_00, 1.0)
    health_norm = ctx.health_score / 100.0
    segment_match = 1.0 if ctx.segment in product.target_segments else 0.0

    match product.product_type:
        case ProductType.SAVINGS:
            # Fixed Deposit / High-Yield Savings prioritize buffer, savings rate, and health
            linear = (
                -0.5
                + 1.8 * savings_norm
                + 1.5 * buffer_norm
                + 1.2 * health_norm
                + 1.0 * segment_match
            )
            # Senior Citizen bonus per ADR-0015
            if product.product_id == FIXED_DEPOSIT_ID and ctx.age_band == "60+":
                linear += 0.8
        case ProductType.INVESTMENT:
            # Investments prioritize strong health, high buffer, and high income
            linear = (
                -1.2
                + 2.2 * buffer_norm
                + 1.8 * health_norm
                + 1.5 * savings_norm
                + 1.2 * income_norm
                + 1.2 * segment_match
            )
        case ProductType.LOAN:
            if product.product_id == REWARDS_CARD_ID:
                linear = (
                    -1.0
                    + 3.0 * disc_norm
                    + 1.5 * income_norm
                    + 1.0 * (1.0 - ctx.emi_ratio)
                    + 1.5 * segment_match
                )
            else:
                # Credit line emphasizes liquidity cushion and manageable existing debt
                linear = (
                    -0.8
                    + 1.0 * disc_norm
                    + 1.6 * income_norm
                    + 1.5 * (1.0 - ctx.emi_ratio)
                    + 1.2 * segment_match
                )
        case ProductType.INSURANCE:
            # Insurance covers liabilities and protection
            linear = (
                -0.2
                + 1.0 * ctx.emi_ratio
                + 1.0 * (1.0 - health_norm)
                + 0.8 * income_norm
                + 0.8 * segment_match
            )

    # Calibrated Sigmoid probability: P = 1 / (1 + e^(-linear))
    score = 1.0 / (1.0 + math.exp(-linear))
    return round(float(min(max(score, 0.01), 0.99)), 4)


def _reasons_for_fixed_deposit(ctx: CustomerRecommendationContext) -> list[str]:
    reasons: list[str] = []
    if ctx.age_band == "60+":
        reasons.append("Eligible for Senior Citizen 8.0% p.a. guaranteed interest rate.")
    reasons.append(
        f"Your liquid cash buffer ({ctx.buffer_months:.1f} months) allows locking in "
        "risk-free fixed compounding."
    )
    if ctx.segment == "HIGH_NET_WORTH":
        reasons.append("Optimal for capital preservation and high-bracket deposit yields.")
    else:
        reasons.append("Guaranteed returns without exposure to market fluctuations.")
    return reasons


def _reasons_for_savings_accelerator(ctx: CustomerRecommendationContext) -> list[str]:
    reasons: list[str] = []
    if ctx.savings_ratio > 0.15:
        pct_str = f"{ctx.savings_ratio * 100:.1f}%"
        reasons.append(f"Accelerates your {pct_str} monthly savings rate with 6.5% APY.")
    else:
        reasons.append("Instant liquidity with automated round-up savings on every purchase.")
    reasons.append("Zero balance maintenance penalty and unlimited free transfers.")
    return reasons


def _reasons_for_credit_line(ctx: CustomerRecommendationContext) -> list[str]:
    return [
        (
            f"Low existing debt commitments ({ctx.emi_ratio * 100:.1f}% EMI-to-income) "
            "qualify you for our lowest APR tier."
        ),
        "Flexible overdraft cushion: borrow as needed and repay anytime.",
    ]


def _reasons_for_rewards_card(ctx: CustomerRecommendationContext) -> list[str]:
    disc_pct = f"{ctx.discretionary_ratio * 100:.1f}%"
    return [
        (
            f"Lifestyle spending ({disc_pct} on dining, shopping & travel) "
            "earns accelerated 3% cashback."
        ),
        "Complimentary airport lounge access and zero international markup.",
    ]


def _reasons_for_index_sip(ctx: CustomerRecommendationContext) -> list[str]:
    return [
        (
            f"High Financial Health Score ({ctx.health_score}/100) and steady surplus "
            "make you a prime candidate for equity wealth building."
        ),
        ("Long-term inflation-beating returns with low-cost Nifty 50 index allocation."),
    ]


def _reasons_for_family_shield(ctx: CustomerRecommendationContext) -> list[str]:
    reasons: list[str] = []
    if ctx.emi_ratio > 0.20:
        reasons.append(
            "Protects active credit and loan obligations against unforeseen emergencies."
        )
    else:
        reasons.append("Comprehensive family medical and term security with cashless claims.")
    reasons.append("Tax-advantaged protection with comprehensive critical illness coverage.")
    return reasons


_REASON_BUILDERS: dict[UUID, Callable[[CustomerRecommendationContext], list[str]]] = {
    FIXED_DEPOSIT_ID: _reasons_for_fixed_deposit,
    SAVINGS_ACCELERATOR_ID: _reasons_for_savings_accelerator,
    CREDIT_LINE_ID: _reasons_for_credit_line,
    REWARDS_CARD_ID: _reasons_for_rewards_card,
    INDEX_SIP_ID: _reasons_for_index_sip,
    FAMILY_SHIELD_ID: _reasons_for_family_shield,
}


def _build_product_reasons(
    product: BankingProduct,
    ctx: CustomerRecommendationContext,
) -> list[str]:
    """Build grounded human-readable reasons explaining the recommendation."""
    builder = _REASON_BUILDERS.get(product.product_id)
    if builder is None:
        return []
    return builder(ctx)[:3]


def generate_recommendations(
    ctx: CustomerRecommendationContext,
    catalogue: tuple[BankingProduct, ...] = PRODUCT_CATALOGUE,
    top_k: int = 3,
    model_version: str = MODEL_VERSION,
) -> list[Recommendation]:
    """Generate top-k personalized product recommendations with explainable reasons.

    Workflow:
    1. Filter catalogue through eligibility rules.
    2. Score propensity for all eligible products.
    3. Sort products by score descending.
    4. Attach grounded human-readable reasons.
    """
    eligible_scored: list[tuple[BankingProduct, float, list[str]]] = []

    for product in catalogue:
        if not evaluate_eligibility(product, ctx):
            continue

        score = _score_product_propensity(product, ctx)
        reasons = _build_product_reasons(product, ctx)
        eligible_scored.append((product, score, reasons))

    # Sort descending by propensity score
    eligible_scored.sort(key=lambda item: item[1], reverse=True)

    recommendations: list[Recommendation] = []
    for product, score, reasons in eligible_scored[:top_k]:
        recommendations.append(
            Recommendation(
                product_id=product.product_id,
                product_type=product.product_type,
                score=score,
                reasons=reasons,
                model_version=model_version,
                title=product.title,
                description=product.description,
                cta_label=product.cta_label,
                cta_path=product.cta_path,
            )
        )

    return recommendations
