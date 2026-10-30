"""Recommendation contract. Every recommendation carries human-readable reasons."""

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProductType(StrEnum):
    LOAN = "LOAN"
    SAVINGS = "SAVINGS"
    INVESTMENT = "INVESTMENT"
    INSURANCE = "INSURANCE"


class Recommendation(BaseModel):
    model_config = ConfigDict(frozen=True)

    product_id: UUID
    product_type: ProductType
    score: float = Field(ge=0, le=1)
    reasons: list[str] = Field(min_length=1)
    model_version: str
    title: str = Field(default="", description="Product title / name")
    description: str = Field(default="", description="Product description")
    cta_label: str = Field(default="Apply Now", description="Call to action button text")
    cta_path: str = Field(default="/apply", description="Application modal navigation path")


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
    cta_label: str = "Apply Now"
    cta_path: str = "/apply"
