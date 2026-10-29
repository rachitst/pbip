"""Service layer for analytics: spending categorization, aggregations, and trend analysis."""

import calendar
from uuid import UUID

from sqlalchemy.orm import Session

from pbip_api.core.db import utcnow
from pbip_api.modules.analytics import repository
from pbip_api.modules.analytics.models import MonthlyCategorySpending
from pbip_api.modules.analytics.policy import verify_account_ownership
from pbip_api.modules.analytics.schemas import (
    CategoryShiftItem,
    CategorySpendingItem,
    MonthlySpendingSummary,
    SpendingBreakdownResponse,
    SpendingTrendsResponse,
)


def _get_or_compute_monthly_aggregates(
    session: Session, account_id: UUID, year: int, month: int
) -> list[MonthlyCategorySpending]:
    """Retrieve pre-aggregated spending or compute live from entries if not yet materialized."""
    records = repository.get_preaggregated_monthly_spending(session, account_id, year, month)
    if not records:
        records = repository.compute_monthly_spending_from_entries(session, account_id, year, month)
        # If it is a past month, persist it to avoid recomputing next time
        now = utcnow()
        is_past_month = (year < now.year) or (year == now.year and month < now.month)
        if is_past_month and records:
            repository.persist_monthly_aggregates(session, account_id, year, month, records)
            session.commit()
    return records


def get_spending_breakdown(
    session: Session,
    customer_id: UUID,
    account_id: UUID,
    year: int | None = None,
    month: int | None = None,
) -> SpendingBreakdownResponse:
    """Get categorized spending breakdown for an account in a given month (BOLA protected)."""
    account = verify_account_ownership(session, customer_id, account_id)

    now = utcnow()
    target_year = year or now.year
    target_month = month or now.month

    records = _get_or_compute_monthly_aggregates(session, account.id, target_year, target_month)

    total_spend = sum(r.total_amount_minor for r in records)
    total_tx_count = sum(r.transaction_count for r in records)

    category_items: list[CategorySpendingItem] = []
    for r in records:
        percentage = (
            round((r.total_amount_minor / total_spend) * 100, 2) if total_spend > 0 else 0.0
        )
        category_items.append(
            CategorySpendingItem(
                category=r.category,
                amount_minor=r.total_amount_minor,
                percentage=percentage,
                transaction_count=r.transaction_count,
            )
        )

    period_str = f"{target_year:04d}-{target_month:02d}"
    return SpendingBreakdownResponse(
        account_id=account.id,
        currency=account.currency,
        period=period_str,
        year=target_year,
        month=target_month,
        total_spend_minor=total_spend,
        transaction_count=total_tx_count,
        categories=category_items,
    )


def get_spending_trends(
    session: Session,
    customer_id: UUID,
    account_id: UUID,
    year: int | None = None,
    month: int | None = None,
) -> SpendingTrendsResponse:
    """Compute month-over-month spending trends, daily burn rate, and category shifts."""
    account = verify_account_ownership(session, customer_id, account_id)

    now = utcnow()
    curr_year = year or now.year
    curr_month = month or now.month

    curr_records = _get_or_compute_monthly_aggregates(session, account.id, curr_year, curr_month)
    curr_total = sum(r.total_amount_minor for r in curr_records)
    curr_count = sum(r.transaction_count for r in curr_records)

    # Days elapsed in current month for burn rate
    days_in_curr_month = calendar.monthrange(curr_year, curr_month)[1]
    is_current_running_month = curr_year == now.year and curr_month == now.month
    days_elapsed = max(now.day, 1) if is_current_running_month else days_in_curr_month
    daily_burn_rate = curr_total // days_elapsed if days_elapsed > 0 else 0

    curr_summary = MonthlySpendingSummary(
        period=f"{curr_year:04d}-{curr_month:02d}",
        year=curr_year,
        month=curr_month,
        total_spend_minor=curr_total,
        transaction_count=curr_count,
        daily_burn_rate_minor=daily_burn_rate,
    )

    # Previous calendar month
    prev_month = curr_month - 1 if curr_month > 1 else 12
    prev_year = curr_year if curr_month > 1 else curr_year - 1

    prev_records = _get_or_compute_monthly_aggregates(session, account.id, prev_year, prev_month)
    has_prev_data = bool(prev_records)
    prev_total = sum(r.total_amount_minor for r in prev_records)
    prev_count = sum(r.transaction_count for r in prev_records)
    days_in_prev_month = calendar.monthrange(prev_year, prev_month)[1]
    prev_burn_rate = prev_total // days_in_prev_month if days_in_prev_month > 0 else 0

    prev_summary: MonthlySpendingSummary | None = None
    if has_prev_data or prev_total > 0:
        prev_summary = MonthlySpendingSummary(
            period=f"{prev_year:04d}-{prev_month:02d}",
            year=prev_year,
            month=prev_month,
            total_spend_minor=prev_total,
            transaction_count=prev_count,
            daily_burn_rate_minor=prev_burn_rate,
        )

    # Month-over-month total change percentage
    mom_change_pct: float | None = None
    if prev_total > 0:
        mom_change_pct = round(((curr_total - prev_total) / prev_total) * 100, 2)
    elif prev_total == 0 and curr_total > 0:
        mom_change_pct = 100.0

    # Burn rate change percentage
    burn_rate_change_pct: float | None = None
    if prev_burn_rate > 0:
        burn_rate_change_pct = round(((daily_burn_rate - prev_burn_rate) / prev_burn_rate) * 100, 2)

    # Category shifts analysis
    curr_cat_map = {r.category: r.total_amount_minor for r in curr_records}
    prev_cat_map = {r.category: r.total_amount_minor for r in prev_records}
    all_categories = sorted(set(curr_cat_map.keys()) | set(prev_cat_map.keys()))

    increasing: list[CategoryShiftItem] = []
    decreasing: list[CategoryShiftItem] = []

    for cat in all_categories:
        c_amt = curr_cat_map.get(cat, 0)
        p_amt = prev_cat_map.get(cat, 0)
        diff = c_amt - p_amt
        shift_pct = round((diff / p_amt) * 100, 2) if p_amt > 0 else (100.0 if c_amt > 0 else 0.0)

        shift_item = CategoryShiftItem(
            category=cat,
            current_amount_minor=c_amt,
            previous_amount_minor=p_amt,
            change_minor=diff,
            change_percentage=shift_pct,
        )

        if diff > 0:
            increasing.append(shift_item)
        elif diff < 0:
            decreasing.append(shift_item)

    increasing.sort(key=lambda x: x.change_minor, reverse=True)
    decreasing.sort(key=lambda x: x.change_minor)

    return SpendingTrendsResponse(
        account_id=account.id,
        currency=account.currency,
        current_month=curr_summary,
        previous_month=prev_summary,
        mom_spend_change_percentage=mom_change_pct,
        daily_burn_rate_minor=daily_burn_rate,
        burn_rate_change_percentage=burn_rate_change_pct,
        top_categories_increasing=increasing[:5],
        top_categories_decreasing=decreasing[:5],
    )
