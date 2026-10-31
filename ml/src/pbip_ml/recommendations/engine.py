"""Product recommendations: eligibility rules, then a rule-based relevance ranking (TASK-054).

1. Eligibility rules run first and exclude products the customer does not qualify for (buffer,
   income, debt ratio, balance, health score), from the customer's real ledger-derived figures.
2. Eligible products are ordered by a fixed, hand-weighted relevance formula. It is NOT a trained
   model and the score is NOT a probability: it only orders products for one customer. No data
   exists yet to train a propensity model on (there are no recorded product take-ups).
3. Each recommendation carries reasons built from the customer's own figures, in INR.
4. ADR-0015: the age band is never used for ranking; it can only add an eligibility reason, and it
   stays "unknown" until consented date of birth exists (TASK-107).
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

MODEL_VERSION = "rules-v1"

SAVINGS_ACCOUNT_ID = UUID("00000000-0000-4000-a000-000000000001")
FIXED_DEPOSIT_ID = UUID("00000000-0000-4000-a000-000000000002")
CREDIT_LINE_ID = UUID("00000000-0000-4000-a000-000000000003")
REWARDS_CARD_ID = UUID("00000000-0000-4000-a000-000000000004")
INDEX_SIP_ID = UUID("00000000-0000-4000-a000-000000000005")
FAMILY_COVER_ID = UUID("00000000-0000-4000-a000-000000000006")

# The catalogue states what each product is and how it is priced, never a rate or limit PBIP does
# not publish: those are quoted on application.
PRODUCT_CATALOGUE: tuple[BankingProduct, ...] = (
    BankingProduct(
        product_id=SAVINGS_ACCOUNT_ID,
        product_type=ProductType.SAVINGS,
        title="Savings account",
        description="A separate savings account to set money aside from everyday spending.",
        target_segments=["BUDGET_CONSCIOUS", "INACTIVE_SAVER"],
        cta_label="Open account",
    ),
    BankingProduct(
        product_id=FIXED_DEPOSIT_ID,
        product_type=ProductType.SAVINGS,
        title="Fixed deposit",
        description="Lock savings away for a fixed term; the rate is shown before you book.",
        min_buffer_months=2.0,
        min_savings_ratio=0.05,
        max_emi_ratio=0.60,
        min_balance_minor=50_000_00,
        min_health_score=40,
        target_segments=["HIGH_NET_WORTH", "BUDGET_CONSCIOUS"],
        cta_label="Book deposit",
    ),
    BankingProduct(
        product_id=CREDIT_LINE_ID,
        product_type=ProductType.LOAN,
        title="Personal credit line",
        description="Borrow as needed up to an agreed limit, subject to a credit check.",
        min_income_minor=25_000_00,
        min_savings_ratio=-0.20,
        max_emi_ratio=0.45,
        min_health_score=30,
        target_segments=["FREQUENT_TRANSACTOR", "DISCRETIONARY_SPENDER"],
        cta_label="Check eligibility",
    ),
    BankingProduct(
        product_id=REWARDS_CARD_ID,
        product_type=ProductType.LOAN,
        title="Rewards credit card",
        description=(
            "A credit card that earns rewards on everyday spending, subject to a credit check."
        ),
        min_income_minor=35_000_00,
        min_buffer_months=0.5,
        min_savings_ratio=0.0,
        max_emi_ratio=0.50,
        min_health_score=40,
        min_discretionary_ratio=0.20,
        target_segments=["DISCRETIONARY_SPENDER", "FREQUENT_TRANSACTOR", "HIGH_NET_WORTH"],
        cta_label="Check eligibility",
    ),
    BankingProduct(
        product_id=INDEX_SIP_ID,
        product_type=ProductType.INVESTMENT,
        title="Index fund SIP",
        description=(
            "A monthly investment in an index fund. Market-linked: returns are not guaranteed."
        ),
        min_income_minor=30_000_00,
        min_buffer_months=3.0,
        min_savings_ratio=0.15,
        max_emi_ratio=0.40,
        min_balance_minor=100_000_00,
        min_health_score=55,
        target_segments=["HIGH_NET_WORTH", "BUDGET_CONSCIOUS"],
        cta_label="Learn more",
    ),
    BankingProduct(
        product_id=FAMILY_COVER_ID,
        product_type=ProductType.INSURANCE,
        title="Family protection cover",
        description="Term life and family health cover; the premium is quoted on application.",
        min_income_minor=15_000_00,
        min_savings_ratio=-0.10,
        max_emi_ratio=0.70,
        target_segments=["FREQUENT_TRANSACTOR", "BUDGET_CONSCIOUS", "DISCRETIONARY_SPENDER"],
        cta_label="Get a quote",
    ),
)


def format_inr(minor: int) -> str:
    """INR with Indian digit grouping, e.g. 2,01,072.00 (pbip_ml must not import pbip_api)."""
    sign = "-" if minor < 0 else ""
    rupees, paise = divmod(abs(minor), 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join([*groups, tail])
    return f"{sign}INR {digits}.{paise:02d}"


def evaluate_eligibility(product: BankingProduct, ctx: CustomerRecommendationContext) -> bool:
    """Hard eligibility rules, applied before any ranking. True if the customer qualifies."""
    return (
        ctx.monthly_inflow_minor >= product.min_income_minor
        and ctx.buffer_months >= product.min_buffer_months
        and ctx.savings_ratio >= product.min_savings_ratio
        and ctx.emi_ratio <= product.max_emi_ratio
        and ctx.total_balance_minor >= product.min_balance_minor
        and ctx.health_score >= product.min_health_score
        and ctx.discretionary_ratio >= product.min_discretionary_ratio
    )


def relevance_score(product: BankingProduct, ctx: CustomerRecommendationContext) -> float:
    """Fixed weighted formula squashed into (0, 1) for ordering. Not trained, not a probability."""
    savings = min(max(ctx.savings_ratio, 0.0), 1.0)
    discretionary = min(max(ctx.discretionary_ratio, 0.0), 1.0)
    buffer = min(ctx.buffer_months / 12.0, 1.0)
    income = min(ctx.monthly_inflow_minor / 300_000_00, 1.0)
    health = ctx.health_score / 100.0
    segment = 1.0 if ctx.segment in product.target_segments else 0.0
    debt_room = 1.0 - min(ctx.emi_ratio, 1.0)

    match product.product_type:
        case ProductType.SAVINGS:
            linear = -0.5 + 1.8 * savings + 1.5 * buffer + 1.2 * health + 1.0 * segment
        case ProductType.INVESTMENT:
            linear = -1.2 + 2.2 * buffer + 1.8 * health + 1.5 * savings + 1.2 * income
            linear += 1.2 * segment
        case ProductType.LOAN if product.product_id == REWARDS_CARD_ID:
            linear = -1.0 + 3.0 * discretionary + 1.5 * income + 1.0 * debt_room + 1.5 * segment
        case ProductType.LOAN:
            linear = -0.8 + 1.0 * discretionary + 1.6 * income + 1.5 * debt_room + 1.2 * segment
        case ProductType.INSURANCE:
            linear = -0.2 + 1.0 * min(ctx.emi_ratio, 1.0) + 1.0 * (1.0 - health) + 0.8 * income
            linear += 0.8 * segment
    return round(1.0 / (1.0 + math.exp(-linear)), 4)


def _savings_reasons(ctx: CustomerRecommendationContext) -> list[str]:
    if ctx.monthly_inflow_minor > ctx.monthly_spend_minor > 0:
        kept = ctx.monthly_inflow_minor - ctx.monthly_spend_minor
        return [
            f"You keep about {format_inr(kept)} of {format_inr(ctx.monthly_inflow_minor)} "
            "income each month; a separate account keeps it apart from spending."
        ]
    return [
        f"Your spending averages {format_inr(ctx.monthly_spend_minor)} a month; a separate "
        "account makes it easier to set money aside."
    ]


def _deposit_reasons(ctx: CustomerRecommendationContext) -> list[str]:
    reasons = [
        f"Your balance of {format_inr(ctx.total_balance_minor)} covers "
        f"{ctx.buffer_months:.1f} months of your average spending.",
        f"Your financial health score is {ctx.health_score}/100.",
    ]
    if ctx.age_band == "60+":  # ADR-0015: an eligibility reason only, never a ranking input
        reasons.append("Senior-citizen deposit rates apply because you are 60 or over.")
    return reasons


def _credit_line_reasons(ctx: CustomerRecommendationContext) -> list[str]:
    return [
        f"Loan repayments take {ctx.emi_ratio * 100:.1f}% of your income of "
        f"{format_inr(ctx.monthly_inflow_minor)} a month, within the 45% limit.",
    ]


def _rewards_card_reasons(ctx: CustomerRecommendationContext) -> list[str]:
    lifestyle = round(ctx.monthly_spend_minor * ctx.discretionary_ratio)
    return [
        f"{ctx.discretionary_ratio * 100:.1f}% of your spending (about {format_inr(lifestyle)} a "
        "month) goes on dining, shopping, entertainment and travel.",
    ]


def _sip_reasons(ctx: CustomerRecommendationContext) -> list[str]:
    return [
        f"Your balance of {format_inr(ctx.total_balance_minor)} covers "
        f"{ctx.buffer_months:.1f} months of spending, above the 3 months we look for first.",
        f"You save {ctx.savings_ratio * 100:.1f}% of your income and your financial health "
        f"score is {ctx.health_score}/100.",
    ]


def _cover_reasons(ctx: CustomerRecommendationContext) -> list[str]:
    if ctx.emi_ratio > 0.20:
        return [
            f"Loan repayments take {ctx.emi_ratio * 100:.1f}% of your income; cover protects "
            "those commitments."
        ]
    return [f"Your income averages {format_inr(ctx.monthly_inflow_minor)} a month."]


_REASONS: dict[UUID, Callable[[CustomerRecommendationContext], list[str]]] = {
    SAVINGS_ACCOUNT_ID: _savings_reasons,
    FIXED_DEPOSIT_ID: _deposit_reasons,
    CREDIT_LINE_ID: _credit_line_reasons,
    REWARDS_CARD_ID: _rewards_card_reasons,
    INDEX_SIP_ID: _sip_reasons,
    FAMILY_COVER_ID: _cover_reasons,
}


def generate_recommendations(
    ctx: CustomerRecommendationContext,
    catalogue: tuple[BankingProduct, ...] = PRODUCT_CATALOGUE,
    top_k: int = 3,
    model_version: str = MODEL_VERSION,
) -> list[Recommendation]:
    """Eligible products only, ordered by relevance, each with reasons from the customer's data."""
    eligible = [product for product in catalogue if evaluate_eligibility(product, ctx)]
    ranked = sorted(eligible, key=lambda product: relevance_score(product, ctx), reverse=True)
    return [
        Recommendation(
            product_id=product.product_id,
            product_type=product.product_type,
            score=relevance_score(product, ctx),
            reasons=_REASONS[product.product_id](ctx)[:3],
            model_version=model_version,
            title=product.title,
            description=product.description,
            cta_label=product.cta_label,
        )
        for product in ranked[:top_k]
    ]
