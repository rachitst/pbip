"""Service layer for analytics: spending breakdown, month-over-month trends, materialisation.

Read path (never writes): a closed month is served from ``monthly_category_spending`` when the
nightly task has stored it; otherwise, and always for the running month, the breakdown is computed
from the ledger entries (ADR-0011). Write path: ``materialize_monthly_spending`` is the Celery
task body that stores closed months and refreshes the most recent one because a transfer can be
reversed weeks after it was posted.
"""

import calendar
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from pbip_api.core.db import utcnow
from pbip_api.core.errors import NotFoundError
from pbip_api.modules.analytics import repository
from pbip_api.modules.analytics.health import compute_financial_health_score
from pbip_api.modules.analytics.models import MonthlyCategorySpending
from pbip_api.modules.analytics.policy import verify_account_ownership
from pbip_api.modules.analytics.schemas import (
    CategoryShiftItem,
    CategorySpendingItem,
    CustomerSegmentResponse,
    FinancialHealthScoreResponse,
    MonthlySpendingSummary,
    SpendingBreakdownResponse,
    SpendingTrendsResponse,
)

MATERIALIZED_MONTHS_BACK = 2
REVERSAL_LOOKBACK = timedelta(
    days=2
)  # overlaps consecutive nightly runs; refreshing twice is harmless


def percentage_change(current: int, previous: int) -> float | None:
    """Relative change in percent; None when there is no previous value to compare against."""
    if previous <= 0:
        return None
    return round((current - previous) / previous * 100, 2)


def is_closed_month(year: int, month: int, now: datetime) -> bool:
    return (year, month) < (now.year, now.month)


def previous_closed_months(now: datetime, count: int) -> list[tuple[int, int]]:
    """The ``count`` calendar months before the running one, most recent first."""
    months: list[tuple[int, int]] = []
    year, month = now.year, now.month
    for _ in range(count):
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
        months.append((year, month))
    return months


def _monthly_aggregates(
    session: Session, account_id: UUID, year: int, month: int
) -> list[MonthlyCategorySpending]:
    if is_closed_month(year, month, utcnow()):
        stored = repository.get_preaggregated_monthly_spending(session, account_id, year, month)
        if stored:
            return stored
    return repository.compute_monthly_spending_from_entries(session, account_id, year, month)


def materialize_monthly_spending(
    session: Session,
    periods: list[tuple[int, int]] | None = None,
    now: datetime | None = None,
    reversed_since: datetime | None = None,
) -> int:
    """Store the per-category spend of every customer account for the given months.

    Defaults to the two most recent closed months, plus the posting month of every transfer
    reversed since ``reversed_since`` (default: the last two days), because a reversal changes a
    month's spend without adding an entry to it. Idempotent (rows are upserted under their unique
    key, vanished categories are deleted) and committed once at the end. Returns the number of
    account-months written. A running month may be passed; the read path ignores stored rows for
    it, so this is only useful to tests.
    """
    now = now or utcnow()
    if periods is None:
        periods = previous_closed_months(now, MATERIALIZED_MONTHS_BACK)
    targets: list[tuple[UUID, int, int]] = [
        (account_id, year, month)
        for year, month in periods
        for account_id in repository.accounts_with_debits(session, year, month)
    ]
    since = reversed_since or now - REVERSAL_LOOKBACK
    targets.extend(
        item
        for item in repository.account_months_reversed_since(session, since)
        if item not in targets
    )
    for account_id, year, month in targets:
        records = repository.compute_monthly_spending_from_entries(session, account_id, year, month)
        repository.upsert_monthly_aggregates(session, account_id, year, month, records)
    session.commit()
    return len(targets)


def get_spending_breakdown(
    session: Session,
    customer_id: UUID,
    account_id: UUID,
    year: int | None = None,
    month: int | None = None,
) -> SpendingBreakdownResponse:
    """Categorised spend of one owned account in a calendar month (default: the running month)."""
    account = verify_account_ownership(session, customer_id, account_id)
    now = utcnow()
    target_year, target_month = year or now.year, month or now.month

    records = _monthly_aggregates(session, account.id, target_year, target_month)
    total_spend = sum(r.total_amount_minor for r in records)
    categories = [
        CategorySpendingItem(
            category=r.category,
            amount_minor=r.total_amount_minor,
            percentage=round(r.total_amount_minor / total_spend * 100, 2) if total_spend else 0.0,
            transaction_count=r.transaction_count,
        )
        for r in records
    ]
    return SpendingBreakdownResponse(
        account_id=account.id,
        currency=account.currency,
        period=f"{target_year:04d}-{target_month:02d}",
        year=target_year,
        month=target_month,
        total_spend_minor=total_spend,
        transaction_count=sum(r.transaction_count for r in records),
        categories=categories,
    )


def _summary(
    records: list[MonthlyCategorySpending], year: int, month: int, now: datetime
) -> MonthlySpendingSummary:
    """Totals of a month; the burn rate divides by the days elapsed so far for the running month."""
    total = sum(r.total_amount_minor for r in records)
    running = (year, month) == (now.year, now.month)
    days = max(now.day, 1) if running else calendar.monthrange(year, month)[1]
    return MonthlySpendingSummary(
        period=f"{year:04d}-{month:02d}",
        year=year,
        month=month,
        total_spend_minor=total,
        transaction_count=sum(r.transaction_count for r in records),
        daily_burn_rate_minor=total // days,
    )


def get_spending_trends(
    session: Session,
    customer_id: UUID,
    account_id: UUID,
    year: int | None = None,
    month: int | None = None,
) -> SpendingTrendsResponse:
    """Month-over-month comparison of one owned account: totals, burn rate and category shifts."""
    account = verify_account_ownership(session, customer_id, account_id)
    now = utcnow()
    curr_year, curr_month = year or now.year, month or now.month
    prev_year, prev_month = previous_closed_months(
        datetime(curr_year, curr_month, 1, tzinfo=now.tzinfo), 1
    )[0]

    curr_records = _monthly_aggregates(session, account.id, curr_year, curr_month)
    prev_records = _monthly_aggregates(session, account.id, prev_year, prev_month)
    current = _summary(curr_records, curr_year, curr_month, now)
    previous = _summary(prev_records, prev_year, prev_month, now) if prev_records else None

    curr_by_cat = {r.category: r.total_amount_minor for r in curr_records}
    prev_by_cat = {r.category: r.total_amount_minor for r in prev_records}
    shifts = [
        CategoryShiftItem(
            category=category,
            current_amount_minor=curr_by_cat.get(category, 0),
            previous_amount_minor=prev_by_cat.get(category, 0),
            change_minor=curr_by_cat.get(category, 0) - prev_by_cat.get(category, 0),
            change_percentage=percentage_change(
                curr_by_cat.get(category, 0), prev_by_cat.get(category, 0)
            ),
        )
        for category in sorted(curr_by_cat.keys() | prev_by_cat.keys())
    ]
    increasing = sorted(
        (s for s in shifts if s.change_minor > 0), key=lambda s: s.change_minor, reverse=True
    )
    decreasing = sorted((s for s in shifts if s.change_minor < 0), key=lambda s: s.change_minor)

    return SpendingTrendsResponse(
        account_id=account.id,
        currency=account.currency,
        current_month=current,
        previous_month=previous,
        mom_spend_change_percentage=percentage_change(
            current.total_spend_minor, previous.total_spend_minor if previous else 0
        ),
        daily_burn_rate_minor=current.daily_burn_rate_minor,
        burn_rate_change_percentage=percentage_change(
            current.daily_burn_rate_minor, previous.daily_burn_rate_minor if previous else 0
        ),
        top_categories_increasing=increasing[:5],
        top_categories_decreasing=decreasing[:5],
    )


def get_financial_health_score(
    session: Session, customer_id: UUID, account_id: UUID
) -> FinancialHealthScoreResponse:
    """Explainable health score of one owned account from its trailing ledger history."""
    account = verify_account_ownership(session, customer_id, account_id)
    metrics = repository.get_financial_health_metrics(session, account.id, utcnow())
    return compute_financial_health_score(
        account.id, account.currency, account.available_balance, metrics
    )


def get_customer_segment(session: Session, customer_id: UUID) -> CustomerSegmentResponse:
    """The segment the offline job assigned to the authenticated customer; 404 until it has run."""
    record = repository.get_customer_segment(session, customer_id)
    if record is None:
        raise NotFoundError("no segment has been assigned to this customer yet")
    return CustomerSegmentResponse(
        customer_id=record.customer_id,
        segment=record.segment,
        confidence_score=record.confidence_score,
        recency_days=record.recency_days,
        frequency=record.frequency,
        monetary_minor=record.monetary_minor,
        savings_ratio=record.savings_ratio,
        discretionary_ratio=record.discretionary_ratio,
        explanation=record.explanation,
        assigned_at=record.updated_at,
    )
