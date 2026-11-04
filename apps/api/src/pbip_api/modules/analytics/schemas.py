"""Pydantic schemas for analytics: spending breakdown and trends."""

from datetime import date as date_type
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


PREDICTED_LABEL: Literal["Predicted, not exact"] = "Predicted, not exact"


class BalanceForecastPoint(BaseModel):
    """One predicted day. Integer paise; the interval says how sure the model is."""

    model_config = ConfigDict(frozen=True)

    date: date_type
    predicted_minor: int = Field(description="Predicted, not exact: the model's central estimate")
    lower_80_minor: int
    upper_80_minor: int
    lower_95_minor: int
    upper_95_minor: int
    obligations_minor: int = Field(
        default=0,
        ge=0,
        description="Known scheduled debits due this day (standing orders, loan EMIs), "
        "already subtracted from the projection",
    )


class BalanceHistoryPoint(BaseModel):
    """A past end-of-day ledger balance: actual, not predicted."""

    model_config = ConfigDict(frozen=True)

    date: date_type
    balance_minor: int


class ForecastObligation(BaseModel):
    """A known upcoming debit: one occurrence of an ACTIVE scheduled instruction (TASK-043)."""

    model_config = ConfigDict(frozen=True)

    date: date_type = Field(description="Day the engine will collect it (overdue EMIs: today)")
    amount_minor: int = Field(gt=0)
    kind: Literal["SCHEDULED_TRANSFER", "LOAN_EMI"]
    scheduled_transfer_id: UUID
    loan_id: UUID | None = None
    reference: str | None = None


class EndOfMonthProjection(BaseModel):
    """The projected balance on the running month's last day (TASK-043). Predicted, not exact."""

    model_config = ConfigDict(frozen=True)

    date: date_type
    predicted_minor: int
    lower_80_minor: int
    upper_80_minor: int
    lower_95_minor: int
    upper_95_minor: int
    obligations_minor: int = Field(
        ge=0, description="Known scheduled debits from today to the month end, included above"
    )
    alert_threshold_minor: int = Field(
        description="INR 1,000.00 above the lowest allowed balance (0 minus the overdraft limit)"
    )
    low_balance: bool = Field(
        description="True when the predicted month-end balance is at or below the threshold"
    )


class BalanceForecastResponse(BaseModel):
    """A projection of the end-of-day balance. Predicted, not exact."""

    model_config = ConfigDict(frozen=True)

    label: Literal["Predicted, not exact"] = Field(
        default=PREDICTED_LABEL, description="Always shown with the numbers"
    )
    status: Literal["OK", "INSUFFICIENT_HISTORY"] = Field(
        default="OK",
        description="INSUFFICIENT_HISTORY: fewer than two days of balance history, so there is no "
        "forecast yet (points and history are empty, end_of_month is null). An expected state of "
        "a new account, not an error",
    )
    account_id: UUID
    currency: str
    method: str = Field(
        description="auto_arima or naive (by history length); none when status "
        "is INSUFFICIENT_HISTORY"
    )
    model_version: str
    history_days: int = Field(description="Days of balance history the forecast used")
    horizon_days: int
    points: list[BalanceForecastPoint] = Field(
        description="Model forecast of everything except scheduled debits, minus the known "
        "obligations (TASK-043)"
    )
    history: list[BalanceHistoryPoint] = Field(
        description="Actual end-of-day balances of the last 30 days before the first point"
    )
    obligations: list[ForecastObligation] = Field(
        description="Known upcoming debits to the later of the horizon end and the month end"
    )
    end_of_month: EndOfMonthProjection | None = Field(
        description="Null only when status is INSUFFICIENT_HISTORY"
    )


# --- Behavioural analytics dashboard (TASK-101): aggregates only, never a customer id ---

BehaviourSegment = Literal[
    "HIGH_NET_WORTH",
    "BUDGET_CONSCIOUS",
    "FREQUENT_TRANSACTOR",
    "DISCRETIONARY_SPENDER",
    "INACTIVE_SAVER",
    "UNSEGMENTED",
]


class SegmentShare(BaseModel):
    """One segment's size and the average RFM features the weekly job stored for it."""

    segment: BehaviourSegment
    customers: int
    share_pct: float = Field(description="Share of all customers, percent, one decimal")
    avg_recency_days: float | None = Field(description="Days since last transaction (job input)")
    avg_monthly_transactions: float | None
    avg_monthly_spend_minor: int | None
    avg_savings_ratio: float | None
    avg_discretionary_ratio: float | None


class SegmentDistribution(BaseModel):
    total_customers: int
    last_segmented_at: datetime | None = Field(description="Latest weekly segmentation write")
    items: list[SegmentShare]


class TypeSpend(BaseModel):
    transaction_type: str
    spend_minor: int
    transactions: int


class SegmentSpend(BaseModel):
    segment: BehaviourSegment
    customers: int
    spenders: int = Field(description="Customers with at least one spend entry in the period")
    spend_minor: int
    transactions: int
    avg_spend_per_spender_minor: int = Field(description="spend_minor // spenders (floor)")
    by_type: list[TypeSpend]


class ProductHolding(BaseModel):
    product: str
    holders: int
    share_pct: float = Field(description="holders / customers in the segment, percent")


class SegmentAffinity(BaseModel):
    segment: BehaviourSegment
    customers: int
    holdings: list[ProductHolding]


class ProductClicks(BaseModel):
    product_id: UUID
    title: str
    product_type: str
    clicks: int = Field(description="product_clicked events in the period")
    customers: int = Field(description="Distinct customers who clicked")


InterestCategory = Literal["LOAN", "SAVINGS", "INVESTMENT", "INSURANCE"]


class CategoryInterest(BaseModel):
    """How many profiled customers show interest in a product category (ADR-0057)."""

    category: InterestCategory
    customers: int = Field(
        description="Profiled customers with this category among their interests"
    )
    share_pct: float = Field(description="customers / profiled customers in scope, percent")
    avg_score: float = Field(description="Mean interest score of those customers (3 decimals)")
    signals: int = Field(
        description="Interest signals behind the scores (clicks, calculator, pages)"
    )


class AffinityPanel(BaseModel):
    products: list[str]
    segments: list[SegmentAffinity]
    recommendation_clicks: list[ProductClicks]
    profiled_customers: int = Field(
        default=0,
        description=(
            "Customers in scope with a behavioural profile (analytics consent on), from the "
            "latest daily profile run over its own 90-day window, not the period filter"
        ),
    )
    interests: list[CategoryInterest] = Field(
        default_factory=list,
        description="Product interest by category across profiled customers, most customers first",
    )


class EventTypeCount(BaseModel):
    event_type: str
    events: int


class SegmentEngagement(BaseModel):
    segment: BehaviourSegment
    customers: int
    active_customers: int
    active_share_pct: float


class DailyEngagement(BaseModel):
    day: date_type
    active_customers: int
    events: int
    sessions: int = Field(default=0, description="Sessions that started on this day (UTC)")
    avg_session_seconds: int = Field(default=0, description="Mean length of those sessions")


class EngagementPanel(BaseModel):
    events: int = Field(description="Interactions in the period (session heartbeats not counted)")
    sessions: int = Field(
        description=(
            "Sessions in the period: events of one session id, split where 30 minutes pass "
            "without an event (ADR-0057)"
        )
    )
    active_customers: int = Field(description="Customers with at least one event in the period")
    customers: int = Field(description="Customers in scope (all, or the filtered segment)")
    consenting_customers: int = Field(
        default=0,
        description="Customers in scope with analytics consent on; only their events count",
    )
    active_share_pct: float
    events_per_session: float
    avg_session_seconds: int = Field(
        description="Mean of last minus first event time per session (one-event sessions are 0)"
    )
    by_event_type: list[EventTypeCount]
    by_segment: list[SegmentEngagement]
    daily: list[DailyEngagement] = Field(description="Days (UTC) with at least one event")


class BehaviourAnalyticsResponse(BaseModel):
    """TASK-101: segment mix, spending, product affinity and engagement; aggregates only."""

    generated_at: datetime
    period_days: int
    period_start: datetime
    period_end: datetime
    segment: BehaviourSegment | None = Field(description="Segment filter, or null for all")
    currency: str
    segments: SegmentDistribution
    spending: list[SegmentSpend]
    affinity: AffinityPanel
    engagement: EngagementPanel


# --- Behavioural profile of one customer (UC-11, ADR-0057) ---


class ProductInterestOut(BaseModel):
    category: InterestCategory
    score: float = Field(
        description="Recency-weighted interest (signal weight halves every 14 days), 3 decimals"
    )
    signals: int = Field(description="Clicks, calculator uses and product-page visits counted")
    last_signal_at: datetime


class ProfileEngagement(BaseModel):
    window_days: int = Field(description="Days of clickstream the profile covers")
    sessions: int
    avg_session_seconds: int = Field(description="Mean session length (first to last event)")
    pages_per_session: float
    interactions: int = Field(description="Events other than session heartbeats")
    last_active_at: datetime | None


class SpendIncomePattern(BaseModel):
    currency: str
    monthly_income_minor: int = Field(description="Average recurring monthly income (ADR-0043)")
    monthly_spend_minor: int = Field(description="Average monthly spend")
    discretionary_ratio: float = Field(description="Share of spend on lifestyle categories")


class BehaviourProfile(BaseModel):
    computed_at: datetime | None = Field(
        description="When the daily job last built the engagement and interests; null before"
    )
    segment: BehaviourSegment = Field(description="Weekly segment, or UNSEGMENTED")
    spend_income: SpendIncomePattern | None = Field(description="Null when no account is held")
    engagement: ProfileEngagement | None = Field(description="Null until the daily job has run")
    product_interests: list[ProductInterestOut] = Field(description="Highest score first")


class BehaviourProfileResponse(BaseModel):
    """UC-11 behavioural profile. Without analytics consent there is no profile at all."""

    analytics_consent: bool
    profile: BehaviourProfile | None = Field(description="Null when analytics consent is off")
