"""Pydantic schemas for personalized banking product recommendations (TASK-054)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProductRecommendationItem(BaseModel):
    """A single personalized product recommendation with explainable reasons."""

    model_config = ConfigDict(frozen=True)

    product_id: UUID = Field(..., description="Unique product catalog identifier")
    product_type: str = Field(
        ..., description="Product category: SAVINGS, LOAN, INVESTMENT, or INSURANCE"
    )
    title: str = Field(..., description="Human-readable product title")
    description: str = Field(..., description="Summary description of the product offer")
    score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Rule-based relevance, for ordering only; not a probability",
    )
    reasons: list[str] = Field(min_length=1, description="Human-readable justification reasons")
    model_version: str = Field(..., description="Recommendation model version identifier")
    cta_label: str = Field(..., description="Call to action button label")


class RecommendationsResponse(BaseModel):
    """Customer personalized product recommendations payload."""

    model_config = ConfigDict(frozen=True)

    customer_id: UUID = Field(..., description="Target customer identifier")
    recommendations: list[ProductRecommendationItem] = Field(
        default_factory=list, description="Top recommended banking products"
    )
    model_version: str = Field(..., description="Recommendation engine version")
    evaluated_at: datetime = Field(..., description="UTC timestamp of the recommendation run")
