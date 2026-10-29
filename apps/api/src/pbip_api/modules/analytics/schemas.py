"""Pydantic schemas for analytics: spending breakdown and trends."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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
    daily_burn_rate_minor: int = Field(..., ge=0, description="Average daily spend in minor units")


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
        None, description="Percentage change (+/-), None if previous was 0"
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
        None, description="Month-over-month total spend percentage change (+/-)"
    )
    daily_burn_rate_minor: int = Field(
        ..., ge=0, description="Current daily burn rate in minor units"
    )
    burn_rate_change_percentage: float | None = Field(
        None, description="Burn rate percentage change vs previous month"
    )
    top_categories_increasing: list[CategoryShiftItem] = Field(
        default_factory=list, description="Categories with highest spending increases"
    )
    top_categories_decreasing: list[CategoryShiftItem] = Field(
        default_factory=list, description="Categories with highest spending decreases"
    )
