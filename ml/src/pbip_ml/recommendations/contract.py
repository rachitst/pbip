"""Recommendation contract. Every recommendation carries human-readable reasons."""

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProductType(StrEnum):
    LOAN = "LOAN"
    SAVINGS = "SAVINGS"
    INVESTMENT = "INVESTMENT"
    INSURANCE = "INSURANCE"


class RiskLevel(StrEnum):
    """A product's risk level, and the customer's RTQ-1 risk band (TASK-097), on one scale.

    The values match ``customers.risk.RiskBand`` and the wealth catalogue's ``risk_level`` so the
    engine and the assistant (TASK-105) apply the same suitability rule (ADR-0056).
    """

    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


class HeldProductKind(StrEnum):
    """What a customer already holds, as the owning modules report it (TASK-111, ADR-0030).

    No fund or card kinds: PBIP sells no fund units (ADR-0025) and issues no cards or insurance,
    so nobody can hold one. A new kind is an additive contract change once a module sells it.
    """

    SAVINGS_ACCOUNT = "SAVINGS_ACCOUNT"
    CURRENT_ACCOUNT = "CURRENT_ACCOUNT"
    PERSONAL_LOAN = "PERSONAL_LOAN"
    AUTO_LOAN = "AUTO_LOAN"
    HOME_LOAN = "HOME_LOAN"
    FIXED_DEPOSIT = "FIXED_DEPOSIT"


class HeldProduct(BaseModel):
    """One product the customer holds now. No ids or account numbers: the rules need neither."""

    model_config = ConfigDict(frozen=True)

    kind: HeldProductKind
    amount_minor: int = Field(
        ge=0,
        default=0,
        description=(
            "Principal of a fixed deposit or outstanding principal of a loan; 0 for accounts, "
            "whose balances already feed total_balance_minor"
        ),
    )


class LoanOffer(BaseModel):
    """UC-06 (ADR-0056): one catalogue loan as the API's loans module assessed it on the
    customer's own income, spending and balance. The engine never re-decides eligibility; it only
    offers an ``eligible`` loan the customer does not already hold and orders it."""

    model_config = ConfigDict(frozen=True)

    product_id: UUID = Field(description="The loan catalogue id (stable)")
    loan_type: Literal["PERSONAL", "AUTO", "HOME"]
    title: str
    description: str
    eligible: bool
    min_amount_minor: int = Field(ge=0, description="Suggested range: from (0 if not eligible)")
    max_amount_minor: int = Field(ge=0, description="Suggested range: up to (0 if not eligible)")
    tenure_months: int = Field(ge=0, description="The tenor the range is sized at")
    emi_minor: int = Field(ge=0, description="EMI at the suggested maximum")
    annual_rate_bps: int = Field(ge=0)
    reasons: tuple[str, ...] = Field(min_length=1, description="The loans module's own reasons")


class Recommendation(BaseModel):
    model_config = ConfigDict(frozen=True)

    product_id: UUID
    product_type: ProductType
    score: float = Field(
        ge=0,
        le=1,
        description=(
            "Ordering score. rules-v1: rule-based relevance, not a probability. "
            "sim-propensity-v2: probability of uptake next month under a SIMULATION, not "
            "real customer behaviour (ADR-0049, ADR-0053)"
        ),
    )
    reasons: list[str] = Field(min_length=1)
    model_version: str
    title: str = Field(default="", description="Product title / name")
    description: str = Field(default="", description="Product description")
    cta_label: str = Field(default="Learn more", description="Call to action button text")
    loan: LoanOffer | None = Field(default=None, description="Set for a catalogue loan (UC-06)")


class CustomerRecommendationContext(BaseModel):
    """Customer features and behavioural profile passed to the recommendation engine."""

    model_config = ConfigDict(frozen=True)

    customer_id: UUID = Field(..., description="Customer unique identifier")
    segment: str = Field(default="BUDGET_CONSCIOUS", description="Assigned behavioral segment name")
    health_score: int = Field(
        ge=0, le=100, default=50, description="Financial health overall score"
    )
    monthly_inflow_minor: int = Field(
        ge=0, default=0, description="Average monthly credits in minor units"
    )
    monthly_spend_minor: int = Field(
        ge=0, default=0, description="Average monthly debits in minor units"
    )
    savings_ratio: float = Field(default=0.0, description="Net savings ratio")
    discretionary_ratio: float = Field(
        ge=0.0, le=1.0, default=0.0, description="Discretionary spending ratio"
    )
    buffer_months: float = Field(
        ge=0.0, default=0.0, description="Emergency buffer coverage in months"
    )
    emi_ratio: float = Field(ge=0.0, default=0.0, description="EMI-to-income debt ratio")
    total_balance_minor: int = Field(
        default=0, description="Total spendable balance across accounts in minor units"
    )
    age_band: str = Field(
        default="unknown",
        description="Derived age band per ADR-0015 (18-25, 26-35, 36-50, 51-59, 60+, unknown)",
    )
    held_products: tuple[HeldProduct, ...] = Field(
        default=(),
        description="Open accounts, active loans and open fixed deposits (TASK-111, ADR-0030)",
    )
    loan_offers: tuple[LoanOffer, ...] = Field(
        default=(),
        description=(
            "Every catalogue loan as the loans module assessed it (UC-06, ADR-0056). When given, "
            "the catalogue loans replace the abstract credit line"
        ),
    )
    risk_band: RiskLevel | None = Field(
        default=None,
        description=(
            "The customer's RTQ-1 risk band (TASK-097); None until the questionnaire is answered. "
            "A suitability rule, never a ranking weight (ADR-0056)"
        ),
    )


class BankingProduct(BaseModel):
    """Banking product definition evaluated by eligibility and propensity models."""

    model_config = ConfigDict(frozen=True)

    product_id: UUID
    product_type: ProductType
    title: str
    description: str
    min_income_minor: int = 0
    min_buffer_months: float = 0.0
    min_savings_ratio: float = -1.0
    max_emi_ratio: float = 1.0
    min_balance_minor: int = 0
    min_health_score: int = 0
    min_discretionary_ratio: float = 0.0
    target_segments: list[str] = Field(default_factory=list)
    risk_level: RiskLevel | None = Field(
        default=None,
        description="Set for investment products: checked against the customer's band (ADR-0056)",
    )
    # No cta_path: PBIP has no product application pages yet, so none is linked.
    cta_label: str = "Learn more"
