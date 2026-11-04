"""Pydantic schemas for personalized banking product recommendations (TASK-054)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RecommendedLoanOut(BaseModel):
    """UC-06 (ADR-0056): the loans module's assessment of a recommended catalogue loan."""

    model_config = ConfigDict(frozen=True)

    loan_type: Literal["PERSONAL", "AUTO", "HOME"]
    eligibility_status: Literal["ELIGIBLE"] = Field(
        description="Only loans the eligibility rules allow are recommended"
    )
    suggested_min_amount_minor: int = Field(description="The product's smallest amount")
    suggested_max_amount_minor: int = Field(
        description="The largest amount the eligibility rules allow at tenure_months"
    )
    tenure_months: int = Field(description="The tenor the range is sized at")
    emi_at_max_minor: int = Field(description="EMI at the suggested maximum")
    annual_rate_bps: int = Field(description="Indicative annual rate in basis points")


class ProductRecommendationItem(BaseModel):
    """A single personalized product recommendation with explainable reasons."""

    model_config = ConfigDict(frozen=True)

    product_id: UUID = Field(..., description="Unique product catalog identifier")
    product_type: str = Field(
        ...,
        description=(
            "Product kind: SAVINGS, LOAN, INVESTMENT, or INSURANCE. With product_id it is stable "
            "across responses, for wiring card actions"
        ),
    )
    title: str = Field(..., description="Human-readable product title")
    description: str = Field(..., description="Summary description of the product offer")
    score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "Ordering score. rules-v1: rule-based relevance, not a probability. "
            "sim-propensity-v2: probability of uptake next month under a SIMULATION of product "
            "uptake, not real customer behaviour (ADR-0049, ADR-0053)"
        ),
    )
    reasons: list[str] = Field(min_length=1, description="Human-readable justification reasons")
    model_version: str = Field(..., description="Recommendation model version identifier")
    cta_label: str = Field(..., description="Call to action button label")
    loan: RecommendedLoanOut | None = Field(
        default=None,
        description=(
            "Set when the product is a catalogue loan (product_id is the loan catalogue id): its "
            "eligibility, suggested range and EMI (UC-06, ADR-0056)"
        ),
    )


class RecommendationsResponse(BaseModel):
    """Customer personalized product recommendations payload."""

    model_config = ConfigDict(frozen=True)

    customer_id: UUID = Field(..., description="Target customer identifier")
    recommendations: list[ProductRecommendationItem] = Field(
        default_factory=list, description="Top recommended banking products"
    )
    model_version: str = Field(
        ..., description="Ordering version: rules-v1 or sim-propensity-v2 (ADR-0049, ADR-0053)"
    )
    score_version: str = Field(
        ...,
        description=(
            "Human-readable label of what ordered the list, e.g. 'sim-propensity-v2 (trained on "
            "simulated uptake, not real customer behaviour)'"
        ),
    )
    score_from_simulated_data: bool = Field(
        ...,
        description=(
            "True when a model trained on SIMULATED product uptake ordered the list; its "
            "measured performance is on simulated data only"
        ),
    )
    evaluated_at: datetime = Field(..., description="UTC timestamp of the recommendation run")
