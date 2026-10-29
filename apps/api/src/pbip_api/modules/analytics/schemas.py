"""Pydantic schemas for analytics: spending breakdown and trends."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

Rating = Literal["EXCELLENT", "GOOD", "FAIR", "NEEDS_ATTENTION"]
Assessment = Literal[
    "OPTIMAL",
    "MODERATE",
    "DEFICIT",
    "VOLATILE",
    "NO_DEBT",
    "BURDENED",
    "CRITICAL",
    "ADEQUATE",
    "LOW",
    "VULNERABLE",
    "INSUFFICIENT_DATA",
]


class CategorySpendingItem(BaseModel):
    """Spending total and percentage for a single spending category."""

    model_config = ConfigDict(frozen=True)

    category: str = Field(..., description="Category name (e.g. groceries, utilities)")
    amount_minor: int = Field(..., ge=0, description="Total spend in minor units (pence/cents)")
    percentage: float = Field(..., ge=0.0, le=100.0, description="Percentage of total spend")
    transaction_count: int = Field(..., ge=0, description="Number of debit transactions")


class SpendingBreakdownResponse(BaseModel):
    """Response containing categorized spending breakdown for an account."""

    model_config = ConfigDict(frozen=True)

    account_id: UUID = Field(..., description="Target account identifier")
    currency: str = Field(..., min_length=3, max_length=3, description="ISO currency code")
    period: str = Field(..., description="Aggregation period formatted as YYYY-MM")
    year: int = Field(..., ge=2000, description="Aggregation year")
    month: int = Field(..., ge=1, le=12, description="Aggregation month (1-12)")
    total_spend_minor: int = Field(..., ge=0, description="Total spending in minor units")
    transaction_count: int = Field(..., ge=0, description="Total debit transactions count")
    categories: list[CategorySpendingItem] = Field(
        default_factory=list, description="Category-wise spending items"
    )


class MonthlySpendingSummary(BaseModel):
    """Summary of spending in a calendar month."""

    model_config = ConfigDict(frozen=True)

    period: str = Field(..., description="Period formatted as YYYY-MM")
    year: int = Field(..., ge=2000, description="Year")
    month: int = Field(..., ge=1, le=12, description="Month (1-12)")
    total_spend_minor: int = Field(..., ge=0, description="Total spend in minor units")
    transaction_count: int = Field(..., ge=0, description="Transaction count")
    daily_burn_rate_minor: int = Field(
        ...,
        ge=0,
        description="Total spend divided by the days of the month (elapsed days when running)",
    )


class CategoryShiftItem(BaseModel):
    """Month-over-month shift in spending for a category."""

    model_config = ConfigDict(frozen=True)

    category: str = Field(..., description="Category name")
    current_amount_minor: int = Field(..., ge=0, description="Current month spend in minor units")
    previous_amount_minor: int = Field(..., ge=0, description="Previous month spend in minor units")
    change_minor: int = Field(
        ..., description="Difference in spend (current - previous) in minor units"
    )
    change_percentage: float | None = Field(
        None, description="Percentage change (+/-); None when the previous month had no spend here"
    )


class SpendingTrendsResponse(BaseModel):
    """Month-over-month spending trends and burn rate comparison."""

    model_config = ConfigDict(frozen=True)

    account_id: UUID = Field(..., description="Target account identifier")
    currency: str = Field(..., min_length=3, max_length=3, description="ISO currency code")
    current_month: MonthlySpendingSummary = Field(..., description="Current month summary")
    previous_month: MonthlySpendingSummary | None = Field(
        None, description="Previous month summary (if available)"
    )
    mom_spend_change_percentage: float | None = Field(
        None,
        description="Month-over-month total spend change (+/-); None if last month had none",
    )
    daily_burn_rate_minor: int = Field(
        ..., ge=0, description="Current daily burn rate in minor units"
    )
    burn_rate_change_percentage: float | None = Field(
        None,
        description="Daily burn rate change vs the previous month (+/-); None when it had no spend",
    )
    top_categories_increasing: list[CategoryShiftItem] = Field(
        default_factory=list, description="Categories with highest spending increases"
    )
    top_categories_decreasing: list[CategoryShiftItem] = Field(
        default_factory=list, description="Categories with highest spending decreases"
    )


class HealthScoreComponent(BaseModel):
    """One scored component of the financial health score, with the metric behind it."""

    model_config = ConfigDict(frozen=True)

    score: int = Field(..., ge=0, description="Points earned")
    max_score: int = Field(..., gt=0, description="Points available (the component weight)")
    weight_percentage: float = Field(..., ge=0.0, le=100.0, description="Weight in percent")
    metric_value: float = Field(..., description="The underlying metric (rate, CV, ratio, months)")
    formatted_value: str = Field(
        ..., description="Human-readable metric, e.g. '24.5%', '4.2 months'"
    )
    assessment: Assessment = Field(
        ..., description="Band of the metric; INSUFFICIENT_DATA when it cannot be computed yet"
    )


class FinancialHealthComponents(BaseModel):
    """Component-wise breakdown of the financial health score."""

    model_config = ConfigDict(frozen=True)

    savings_rate: HealthScoreComponent = Field(..., description="Savings rate (30 points)")
    expense_volatility: HealthScoreComponent = Field(
        ..., description="Expense volatility (25 points)"
    )
    emi_to_income: HealthScoreComponent = Field(
        ..., description="Loan/EMI repayments to income (25 points)"
    )
    buffer_months: HealthScoreComponent = Field(
        ..., description="Cash buffer in months (20 points)"
    )


class FinancialHealthScoreResponse(BaseModel):
    """Financial health score (0-100) with explainable components and actionable tips."""

    model_config = ConfigDict(frozen=True)

    account_id: UUID = Field(..., description="Target account identifier")
    currency: str = Field(..., min_length=3, max_length=3, description="ISO currency code")
    overall_score: int = Field(..., ge=0, le=100, description="Sum of the component points")
    rating: Rating = Field(
        ..., description="EXCELLENT >= 80, GOOD >= 60, FAIR >= 40, else NEEDS_ATTENTION"
    )
    insufficient_data: bool = Field(
        ..., description="True when at least one component could not be computed from the history"
    )
    months_observed: int = Field(..., ge=0, description="Calendar months of history in the window")
    window_months: int = Field(..., gt=0, description="Length of the trailing window used")
    components: FinancialHealthComponents = Field(..., description="Component breakdown")
    recommendations: list[str] = Field(default_factory=list, description="Actionable tips")


SegmentName = Literal[
    "HIGH_NET_WORTH",
    "BUDGET_CONSCIOUS",
    "FREQUENT_TRANSACTOR",
    "DISCRETIONARY_SPENDER",
    "INACTIVE_SAVER",
]


class CustomerSegmentResponse(BaseModel):
    """The customer's behavioural segment and the RFM features it was derived from."""

    model_config = ConfigDict(frozen=True)

    customer_id: UUID = Field(..., description="Customer identifier (the principal's own)")
    segment: SegmentName = Field(..., description="Assigned behavioural segment")
    confidence_score: float = Field(
        ..., ge=0.0, le=1.0, description="Softmax of the distances to the cluster centroids"
    )
    recency_days: int = Field(..., ge=0, description="Days since the last ledger entry")
    frequency: float = Field(..., ge=0.0, description="Debits per 30-day month")
    monetary_minor: int = Field(..., ge=0, description="Spend per 30-day month, minor units")
    savings_ratio: float = Field(
        ..., description="(credits - debits) / credits; negative = deficit"
    )
    discretionary_ratio: float = Field(
        ..., ge=0.0, le=1.0, description="Share of spend in dining, shopping, entertainment, travel"
    )
    explanation: str = Field(..., description="Human-readable justification of the segment")
    assigned_at: datetime = Field(..., description="When the offline job assigned the segment")


class SegmentationJobResponse(BaseModel):
    """Summary of one offline segmentation run (the cluster distribution report)."""

    model_config = ConfigDict(frozen=True)

    n_clusters: int = Field(..., ge=0, description="Clusters formed; 0 when the run was skipped")
    n_samples: int = Field(..., ge=0, description="Customers with ledger activity")
    silhouette_score: float | None = Field(
        None, description="Silhouette score; None when fewer than two clusters formed"
    )
    davies_bouldin_index: float | None = Field(
        None, ge=0.0, description="Davies-Bouldin index; None when fewer than two clusters formed"
    )
    inertia: float | None = Field(None, ge=0.0, description="Within-cluster sum of squares")
    cluster_distribution: dict[str, int] = Field(description="Customers per segment")
    cluster_centroids: dict[str, dict[str, float]] = Field(
        description="Centroids per segment in original feature units"
    )
    total_customers_assigned: int = Field(..., ge=0, description="Rows written")
    skipped_reason: str | None = Field(None, description="Why nothing was clustered, if so")
