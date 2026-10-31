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
    """Behavioral profile, churn signals, and product propensity for a single customer."""

    model_config = ConfigDict(frozen=True)

    customer_id: UUID = Field(..., description="Unique customer identifier")
    full_name: str = Field(..., description="Customer full name")
    email: str = Field(..., description="Customer email address")
    segment: str = Field(..., description="Behavioral segment (e.g. HIGH_NET_WORTH)")
    total_balance_minor: int = Field(
        ..., description="Current spendable balance across all accounts in minor units"
    )
    inactivity_days: int = Field(
        ..., ge=0, description="Elapsed days since the last customer activity or transaction"
    )
    balance_decline_rate: float = Field(
        ..., ge=0.0, le=1.0, description="Normalized balance decline rate over the last 30-60 days"
    )
    churn_risk_score: float = Field(
        ..., ge=0.0, le=1.0, description="Calculated churn risk score (0.0 to 1.0)"
    )
    churn_risk_level: ChurnRiskLevel = Field(
        ..., description="Categorical churn risk rating: LOW, MEDIUM, or HIGH"
    )
    top_recommended_product: str | None = Field(
        default=None, description="Title of top propensity banking product"
    )
    top_propensity_score: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Propensity score of the top recommended product"
    )
    high_value_transactor: bool = Field(
        default=False, description="Flag indicating if customer conducts high-value transactions"
    )


class CustomerInsightsListResponse(BaseModel):
    """Paginated collection of prioritized customer insights for Relationship Managers."""

    model_config = ConfigDict(frozen=True)

    items: list[CustomerInsightItem] = Field(
        default_factory=list, description="Prioritized list of customer insight profiles"
    )
    total: int = Field(..., ge=0, description="Total matching customers count")
    evaluated_at: datetime = Field(..., description="Server evaluation timestamp in UTC")
