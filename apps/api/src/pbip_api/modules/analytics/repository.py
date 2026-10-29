"""Repository for analytics: queries pre-aggregated monthly tables and ledger entries."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pbip_ml.segmentation.contract import (
    CustomerRFMFeatures,
    SegmentAssignment,
)
from sqlalchemy import delete, extract, func, select
from sqlalchemy.orm import Session

from pbip_api.modules.accounts.models import AccountHolder
from pbip_api.modules.analytics.categorization import categorize_transaction
from pbip_api.modules.analytics.models import CustomerSegment, MonthlyCategorySpending
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.ledger.models import EntryDirection, Transaction, TransactionEntry


def get_preaggregated_monthly_spending(
    session: Session, account_id: UUID, year: int, month: int
) -> list[MonthlyCategorySpending]:
    """Retrieve pre-aggregated spending by category for an account and month."""
    stmt = (
        select(MonthlyCategorySpending)
        .where(
            MonthlyCategorySpending.account_id == account_id,
            MonthlyCategorySpending.year == year,
            MonthlyCategorySpending.month == month,
        )
        .order_by(MonthlyCategorySpending.total_amount_minor.desc())
    )
    return list(session.execute(stmt).scalars().all())


def compute_monthly_spending_from_entries(
    session: Session, account_id: UUID, year: int, month: int
) -> list[MonthlyCategorySpending]:
    """Compute category aggregations on the fly from immutable ledger entries and transactions."""
    start_dt = datetime(year, month, 1, 0, 0, 0, tzinfo=UTC)
    if month == 12:
        end_dt = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=UTC)
    else:
        end_dt = datetime(year, month + 1, 1, 0, 0, 0, tzinfo=UTC)

    # Fetch all posted debits for this account in the requested calendar window
    stmt = (
        select(
            TransactionEntry.amount,
            Transaction.reference,
            Transaction.type,
        )
        .join(Transaction, Transaction.id == TransactionEntry.transaction_id)
        .where(
            TransactionEntry.account_id == account_id,
            TransactionEntry.direction == EntryDirection.DEBIT,
            TransactionEntry.created_at >= start_dt,
            TransactionEntry.created_at < end_dt,
        )
    )

    rows = session.execute(stmt).all()

    category_totals: dict[str, int] = {}
    category_counts: dict[str, int] = {}

    for amount, reference, tx_type in rows:
        cat = categorize_transaction(reference, str(tx_type) if tx_type else None)
        category_totals[cat] = category_totals.get(cat, 0) + int(amount)
        category_counts[cat] = category_counts.get(cat, 0) + 1

    records: list[MonthlyCategorySpending] = []
    for cat, total in category_totals.items():
        records.append(
            MonthlyCategorySpending(
                account_id=account_id,
                year=year,
                month=month,
                category=cat,
                total_amount_minor=total,
                transaction_count=category_counts.get(cat, 0),
            )
        )

    records.sort(key=lambda r: r.total_amount_minor, reverse=True)
    return records


def persist_monthly_aggregates(
    session: Session,
    account_id: UUID,
    year: int,
    month: int,
    records: list[MonthlyCategorySpending],
) -> None:
    """Store or refresh pre-aggregated monthly category spending."""
    session.execute(
        delete(MonthlyCategorySpending).where(
            MonthlyCategorySpending.account_id == account_id,
            MonthlyCategorySpending.year == year,
            MonthlyCategorySpending.month == month,
        )
    )
    for record in records:
        session.add(record)
    session.flush()


def get_financial_health_metrics(
    session: Session, account_id: UUID
) -> tuple[int, int, list[int], int]:
    """Retrieve historical total credits, debits, monthly debit breakdown, and EMI debits."""
    credit_stmt = select(func.coalesce(func.sum(TransactionEntry.amount), 0)).where(
        TransactionEntry.account_id == account_id,
        TransactionEntry.direction == EntryDirection.CREDIT,
    )
    total_credits = int(session.execute(credit_stmt).scalar_one())

    debit_stmt = select(func.coalesce(func.sum(TransactionEntry.amount), 0)).where(
        TransactionEntry.account_id == account_id,
        TransactionEntry.direction == EntryDirection.DEBIT,
    )
    total_debits = int(session.execute(debit_stmt).scalar_one())

    year_col = extract("year", TransactionEntry.created_at)
    month_col = extract("month", TransactionEntry.created_at)
    monthly_stmt = (
        select(func.coalesce(func.sum(TransactionEntry.amount), 0))
        .where(
            TransactionEntry.account_id == account_id,
            TransactionEntry.direction == EntryDirection.DEBIT,
        )
        .group_by(year_col, month_col)
        .order_by(year_col, month_col)
    )
    monthly_debits = [int(x) for x in session.execute(monthly_stmt).scalars().all()]

    emi_stmt = (
        select(TransactionEntry.amount, Transaction.reference)
        .join(Transaction, Transaction.id == TransactionEntry.transaction_id)
        .where(
            TransactionEntry.account_id == account_id,
            TransactionEntry.direction == EntryDirection.DEBIT,
        )
    )
    emi_rows = session.execute(emi_stmt).all()
    total_emi = 0
    for amt, ref in emi_rows:
        if ref and any(term in ref.lower() for term in ("emi", "loan", "mortgage", "installment")):
            total_emi += int(amt)

    return total_credits, total_debits, monthly_debits, total_emi


def get_customer_segment(session: Session, customer_id: UUID) -> CustomerSegment | None:
    """Retrieve persisted customer behavioral segment profile."""
    stmt = select(CustomerSegment).where(CustomerSegment.customer_id == customer_id)
    return session.execute(stmt).scalars().first()


def upsert_customer_segment(
    session: Session, assignment: SegmentAssignment, rfm: CustomerRFMFeatures
) -> CustomerSegment:
    """Persist or update customer behavioral segment in the database."""
    existing = get_customer_segment(session, assignment.customer_id)
    if existing:
        existing.segment = assignment.segment.value
        existing.confidence_score = assignment.confidence_score
        existing.recency_days = rfm.recency_days
        existing.frequency = rfm.frequency
        existing.monetary_minor = rfm.monetary_minor
        existing.savings_ratio = rfm.savings_ratio
        existing.discretionary_ratio = rfm.discretionary_ratio
        existing.explanation = assignment.explanation
        existing.updated_at = assignment.assigned_at
        session.flush()
        return existing

    record = CustomerSegment(
        customer_id=assignment.customer_id,
        segment=assignment.segment.value,
        confidence_score=assignment.confidence_score,
        recency_days=rfm.recency_days,
        frequency=rfm.frequency,
        monetary_minor=rfm.monetary_minor,
        savings_ratio=rfm.savings_ratio,
        discretionary_ratio=rfm.discretionary_ratio,
        explanation=assignment.explanation,
        created_at=assignment.assigned_at,
        updated_at=assignment.assigned_at,
    )
    session.add(record)
    session.flush()
    return record


DISCRETIONARY_CATEGORIES = frozenset({"dining", "shopping", "entertainment", "travel"})


def _aggregate_rfm_entries(
    entries: Sequence[Any],
) -> tuple[int, int, int, int, datetime | None, datetime | None]:
    """Aggregate total credits, debits, counts, and active period bounds."""
    total_credits = 0
    total_debits = 0
    debit_count = 0
    discretionary_debits = 0
    earliest_dt: datetime | None = None
    most_recent_dt: datetime | None = None

    for amt, direction, created_at_val, ref, tx_type in entries:
        if direction == EntryDirection.CREDIT:
            total_credits += int(amt)
        elif direction == EntryDirection.DEBIT:
            total_debits += int(amt)
            debit_count += 1
            if categorize_transaction(ref, tx_type) in DISCRETIONARY_CATEGORIES:
                discretionary_debits += int(amt)

        if created_at_val:
            if earliest_dt is None or created_at_val < earliest_dt:
                earliest_dt = created_at_val
            if most_recent_dt is None or created_at_val > most_recent_dt:
                most_recent_dt = created_at_val

    return (
        total_credits,
        total_debits,
        debit_count,
        discretionary_debits,
        earliest_dt,
        most_recent_dt,
    )


def extract_customer_rfm_features(
    session: Session, customer_id: UUID, as_of: datetime | None = None
) -> CustomerRFMFeatures:
    """Extract recency, frequency, monetary and lifestyle spending features for a customer."""
    ref_time = as_of or datetime.now(UTC)

    # 1. Get customer accounts
    acc_stmt = select(AccountHolder.account_id).where(AccountHolder.customer_id == customer_id)
    account_ids = list(session.execute(acc_stmt).scalars().all())

    if not account_ids:
        return CustomerRFMFeatures(
            customer_id=customer_id,
            recency_days=90,
            frequency=0.0,
            monetary_minor=0,
            savings_ratio=0.0,
            discretionary_ratio=0.0,
        )

    # 2. Query transactions
    tx_stmt = (
        select(
            TransactionEntry.amount,
            TransactionEntry.direction,
            TransactionEntry.created_at,
            Transaction.reference,
            Transaction.type,
        )
        .join(Transaction, Transaction.id == TransactionEntry.transaction_id)
        .where(TransactionEntry.account_id.in_(account_ids))
        .order_by(TransactionEntry.created_at.asc())
    )
    entries = list(session.execute(tx_stmt).all())

    if not entries:
        return CustomerRFMFeatures(
            customer_id=customer_id,
            recency_days=90,
            frequency=0.0,
            monetary_minor=0,
            savings_ratio=0.0,
            discretionary_ratio=0.0,
        )

    (
        total_credits,
        total_debits,
        debit_count,
        discretionary_debits,
        earliest_dt,
        most_recent_dt,
    ) = _aggregate_rfm_entries(entries)

    recency_days = max((ref_time - most_recent_dt).days, 0) if most_recent_dt else 90

    # Activity span in 30-day months (minimum 1 month)
    if earliest_dt and most_recent_dt:
        span_days = max((ref_time - earliest_dt).days, 1)
        months = max(span_days / 30.0, 1.0)
    else:
        months = 1.0

    frequency = round(debit_count / months, 2)
    monetary_minor = round(total_debits / months)
    savings_ratio = (
        round((total_credits - total_debits) / total_credits, 4) if total_credits > 0 else 0.0
    )
    discretionary_ratio = round(discretionary_debits / total_debits, 4) if total_debits > 0 else 0.0

    return CustomerRFMFeatures(
        customer_id=customer_id,
        recency_days=recency_days,
        frequency=frequency,
        monetary_minor=monetary_minor,
        savings_ratio=savings_ratio,
        discretionary_ratio=discretionary_ratio,
    )


def extract_all_customers_rfm_features(
    session: Session, as_of: datetime | None = None
) -> list[CustomerRFMFeatures]:
    """Extract RFM features across all registered customers in the bank."""
    cust_stmt = select(Customer.id).order_by(Customer.id)
    customer_ids = list(session.execute(cust_stmt).scalars().all())
    return [
        extract_customer_rfm_features(session, cust_id, as_of=as_of) for cust_id in customer_ids
    ]
