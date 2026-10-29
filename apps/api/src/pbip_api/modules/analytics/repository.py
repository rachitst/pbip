"""Repository for analytics: queries pre-aggregated monthly tables and ledger entries."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from pbip_api.modules.analytics.categorization import categorize_transaction
from pbip_api.modules.analytics.models import MonthlyCategorySpending
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
