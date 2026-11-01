"""Pydantic schemas for Relationship Manager behavioral insights (TASK-056)."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from pbip_api.modules.analytics.schemas import Rating
from pbip_api.modules.insights.models import (
    MAX_NOTE_CHARS,
    InteractionChannel,
    InteractionOutcome,
)


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
    loan_product: str | None = Field(
        default=None, description="Best eligible loan or credit product, if any is eligible"
    )
    loan_propensity_score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Rule-based relevance of that loan product (not a probability)",
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


class HealthSummary(BaseModel):
    """Financial health score of the customer's primary account (the one recommendations use)."""

    model_config = ConfigDict(frozen=True)

    overall_score: int = Field(..., ge=0, le=100)
    rating: Rating
    insufficient_data: bool = Field(..., description="Fewer than three months of history")


class TransactionVolumeOut(BaseModel):
    """Posted money movements with other parties; own-account moves and reversals excluded."""

    model_config = ConfigDict(frozen=True)

    window_days: int = Field(..., gt=0)
    transaction_count: int = Field(..., ge=0)
    inflow_minor: int = Field(..., ge=0, description="Money received, minor units")
    outflow_minor: int = Field(..., ge=0, description="Money paid out, minor units")


class RecommendedProduct(BaseModel):
    """An eligible product with the rule-based reasons computed from the customer's own data."""

    model_config = ConfigDict(frozen=True)

    title: str
    product_type: str = Field(..., description="SAVINGS, LOAN, INVESTMENT or INSURANCE")
    score: float = Field(
        ..., ge=0.0, le=1.0, description="Rule-based relevance (not a probability)"
    )
    reasons: list[str]


class CustomerInsightDetail(BaseModel):
    """One customer for the RM flyout: churn signals, health, recent volume and eligible products.

    Same data minimisation as the list: no email, phone, address, birth date or account number.
    """

    model_config = ConfigDict(frozen=True)

    insight: CustomerInsightItem
    customer_since: datetime
    health: HealthSummary | None = Field(
        default=None, description="Null when the customer has no account yet"
    )
    transaction_volume: TransactionVolumeOut
    recommendations: list[RecommendedProduct] = Field(
        default_factory=list, description="Every eligible product, best first"
    )
    evaluated_at: datetime


NoteText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_NOTE_CHARS)
]


class InteractionCreate(BaseModel):
    """A call, meeting, email or note an RM records against a customer."""

    model_config = ConfigDict(extra="forbid")

    channel: InteractionChannel
    outcome: InteractionOutcome
    note: NoteText


class InteractionOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    channel: InteractionChannel
    outcome: InteractionOutcome
    note: str
    logged_by: str = Field(..., description="Email of the staff member who recorded it")
    created_at: datetime


class InteractionListResponse(BaseModel):
    """The newest interactions first; ``total`` counts all of them for this customer."""

    model_config = ConfigDict(frozen=True)

    items: list[InteractionOut]
    total: int = Field(..., ge=0)
