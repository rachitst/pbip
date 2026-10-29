"""Service layer for analytics: spending categorization, aggregations, and trend analysis."""

import calendar
from uuid import UUID

from pbip_ml.segmentation import cluster_customers
from pbip_ml.segmentation.contract import (
    CustomerSegmentName,
    SegmentAssignment,
)
from sqlalchemy.orm import Session

from pbip_api.core.db import utcnow
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
    SegmentationJobResponse,
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


def get_financial_health_score(
    session: Session,
    customer_id: UUID,
    account_id: UUID,
) -> FinancialHealthScoreResponse:
    """Calculate dynamic customer financial health score from ledger history (BOLA protected)."""
    account = verify_account_ownership(session, customer_id, account_id)
    (
        total_credits,
        total_debits,
        monthly_debits,
        total_emi,
    ) = repository.get_financial_health_metrics(session, account.id)

    return compute_financial_health_score(
        account_id=account.id,
        currency=account.currency,
        available_balance=account.available_balance,
        total_credits=total_credits,
        total_debits=total_debits,
        monthly_debit_totals=monthly_debits,
        total_emi_debits=total_emi,
    )


def get_customer_segment(
    session: Session,
    customer_id: UUID,
) -> CustomerSegmentResponse:
    """Retrieve customer behavioral segment or perform initial on-the-fly profiling."""
    record = repository.get_customer_segment(session, customer_id)
    if record:
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

    # Initial assignment if offline clustering hasn't run yet
    rfm = repository.extract_customer_rfm_features(session, customer_id)
    now = utcnow()

    # Rule-based initial assignment
    if rfm.monetary_minor > 200_000_00:  # > 200,000 INR
        segment = CustomerSegmentName.HIGH_NET_WORTH
        explanation = (
            f"High monthly expenditures ({rfm.monetary_minor // 100:,} major units) "
            f"indicate premium wealth capacity."
        )
    elif rfm.frequency > 25.0:
        segment = CustomerSegmentName.FREQUENT_TRANSACTOR
        explanation = (
            f"High transactional frequency ({rfm.frequency:.1f} tx/mo) "
            "across daily payment channels."
        )
    elif rfm.discretionary_ratio > 0.45:
        segment = CustomerSegmentName.DISCRETIONARY_SPENDER
        explanation = (
            f"Significant lifestyle spending ({rfm.discretionary_ratio * 100:.1f}%) "
            f"in dining and shopping."
        )
    elif rfm.recency_days > 45:
        segment = CustomerSegmentName.INACTIVE_SAVER
        explanation = (
            f"Dormant activity profile with {rfm.recency_days} days since last transaction."
        )
    else:
        segment = CustomerSegmentName.BUDGET_CONSCIOUS
        explanation = "Balanced spending habits focused primarily on essentials and steady savings."

    assignment = SegmentAssignment(
        customer_id=customer_id,
        segment=segment,
        confidence_score=0.85,
        assigned_at=now,
        explanation=explanation,
    )
    persisted = repository.upsert_customer_segment(session, assignment, rfm)
    session.commit()

    return CustomerSegmentResponse(
        customer_id=persisted.customer_id,
        segment=persisted.segment,
        confidence_score=persisted.confidence_score,
        recency_days=persisted.recency_days,
        frequency=persisted.frequency,
        monetary_minor=persisted.monetary_minor,
        savings_ratio=persisted.savings_ratio,
        discretionary_ratio=persisted.discretionary_ratio,
        explanation=persisted.explanation,
        assigned_at=persisted.updated_at,
    )


def run_offline_segmentation_job(
    session: Session,
    n_clusters: int = 5,
) -> SegmentationJobResponse:
    """Run batch offline clustering job across all bank customers and persist results."""
    all_rfm = repository.extract_all_customers_rfm_features(session)
    if not all_rfm:
        return SegmentationJobResponse(
            n_clusters=0,
            n_samples=0,
            silhouette_score=0.0,
            davies_bouldin_index=0.0,
            inertia=0.0,
            cluster_distribution={},
            cluster_centroids={},
            total_customers_assigned=0,
        )

    assignments, report = cluster_customers(all_rfm, n_clusters=n_clusters)

    rfm_map = {f.customer_id: f for f in all_rfm}
    assigned_count = 0
    for assign in assignments:
        rfm = rfm_map.get(assign.customer_id)
        if rfm:
            repository.upsert_customer_segment(session, assign, rfm)
            assigned_count += 1

    session.commit()

    return SegmentationJobResponse(
        n_clusters=report.n_clusters,
        n_samples=report.n_samples,
        silhouette_score=report.silhouette_score,
        davies_bouldin_index=report.davies_bouldin_index,
        inertia=report.inertia,
        cluster_distribution=report.cluster_distribution,
        cluster_centroids=report.cluster_centroids,
        total_customers_assigned=assigned_count,
    )
