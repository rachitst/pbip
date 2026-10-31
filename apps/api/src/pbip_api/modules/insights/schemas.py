"""Pydantic schemas for Relationship Manager behavioral insights (TASK-056)."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ChurnRiskLevel(StrEnum):
    """Categorical classification of customer churn risk."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class CustomerInsightItem(BaseModel):
    """What an RM needs to prioritise outreach: no email, address, birth date or account number."""

    model_config = ConfigDict(frozen=True)

    customer_id: UUID = Field(..., description="Unique customer identifier")
    full_name: str = Field(..., description="Customer full name")
    segment: str = Field(..., description="Behavioral segment (e.g. HIGH_NET_WORTH)")
    total_balance_minor: int = Field(
        ..., description="Current spendable balance across all accounts in minor units"
    )
    inactivity_days: int = Field(
        ..., ge=0, description="Elapsed days since the last customer activity or transaction"
    )
    balance_decline_rate: float = Field(
        ..., ge=0.0, le=1.0, description="Share of the balance lost over the last 30 days"
    )
    churn_risk_score: float = Field(
        ..., ge=0.0, le=1.0, description="Rule-based: 55% balance decline + 45% inactivity"
    )
    churn_risk_level: ChurnRiskLevel = Field(
        ..., description="Categorical churn risk rating: LOW, MEDIUM, or HIGH"
    )
    top_recommended_product: str | None = Field(
        default=None, description="Top eligible product from the recommendation rules"
    )
    top_product_score: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Rule-based relevance (not a probability)"
    )
    high_value: bool = Field(
        default=False, description="Balance of INR 1,00,000 or more, or the HIGH_NET_WORTH segment"
    )


class CustomerInsightsListResponse(BaseModel):
    """Paginated collection of prioritized customer insights for Relationship Managers."""

    model_config = ConfigDict(frozen=True)

    items: list[CustomerInsightItem] = Field(
        default_factory=list, description="Prioritized list of customer insight profiles"
    )
    total: int = Field(..., ge=0, description="Total matching customers count")
    evaluated_at: datetime = Field(..., description="Server evaluation timestamp in UTC")
