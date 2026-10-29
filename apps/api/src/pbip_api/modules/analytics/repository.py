"""Repository for analytics: the materialised monthly table and the ledger entries it is built from.

The ledger is read through its models only (no ledger internals are called) and never written;
the only table this module writes is ``monthly_category_spending``.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from pbip_api.modules.accounts.models import Account, AccountType
from pbip_api.modules.analytics.categorization import categorize_transaction, is_debt_repayment
from pbip_api.modules.analytics.models import MonthlyCategorySpending
from pbip_api.modules.ledger.models import (
    EntryDirection,
    Transaction,
    TransactionEntry,
    TransactionStatus,
    TransactionType,
)

HEALTH_WINDOW_MONTHS = 12
INCOME_TYPES = (TransactionType.DEPOSIT, TransactionType.TRANSFER)


@dataclass(frozen=True)
class HealthMetrics:
    """Ledger facts behind the financial health score, over the trailing window (minor units)."""

    income_minor: int  # CREDIT entries of POSTED deposits and transfers (refunds excluded)
    spend_minor: int  # DEBIT entries of POSTED non-REVERSAL transactions (as in the breakdown)
    monthly_spend_minor: list[int]  # spend per calendar month that had any, oldest first
    debt_repayments_minor: int  # the part of spend whose reference marks a loan or EMI instalment
    months_observed: int  # calendar months from the first entry in the window to now; 0 if none


def window_start(now: datetime, months: int) -> datetime:
    """First instant of the calendar month ``months - 1`` months before ``now`` (UTC)."""
    index = now.year * 12 + (now.month - 1) - (months - 1)
    return datetime(index // 12, index % 12 + 1, 1, tzinfo=UTC)


def get_financial_health_metrics(
    session: Session, account_id: UUID, now: datetime
) -> HealthMetrics:
    """One pass over the account's entries in the window (which bounds the work per request)."""
    start = window_start(now, HEALTH_WINDOW_MONTHS)
    stmt = (
        select(
            TransactionEntry.amount,
            TransactionEntry.direction,
            TransactionEntry.created_at,
            Transaction.type,
            Transaction.reference,
        )
        .join(Transaction, Transaction.id == TransactionEntry.transaction_id)
        .where(
            TransactionEntry.account_id == account_id,
            TransactionEntry.created_at >= start,
            Transaction.status == TransactionStatus.POSTED,
            Transaction.type != TransactionType.REVERSAL,
        )
        .order_by(TransactionEntry.created_at)
    )
    income = spend = debt = 0
    monthly: dict[tuple[int, int], int] = {}
    first: datetime | None = None
    for amount, direction, created_at, tx_type, reference in session.execute(stmt).all():
        first = first or created_at
        if direction == EntryDirection.CREDIT:
            if tx_type in INCOME_TYPES:
                income += int(amount)
            continue
        spend += int(amount)
        stamp = created_at.astimezone(UTC)
        monthly[(stamp.year, stamp.month)] = monthly.get((stamp.year, stamp.month), 0) + int(amount)
        if is_debt_repayment(reference):
            debt += int(amount)
    months_observed = 0
    if first is not None:
        begin = first.astimezone(UTC)
        months_observed = (now.year - begin.year) * 12 + (now.month - begin.month) + 1
    return HealthMetrics(
        income_minor=income,
        spend_minor=spend,
        monthly_spend_minor=[monthly[key] for key in sorted(monthly)],
        debt_repayments_minor=debt,
        months_observed=months_observed,
    )


def month_window(year: int, month: int) -> tuple[datetime, datetime]:
    """[start, end) of a calendar month in UTC; ledger timestamps are UTC."""
    start = datetime(year, month, 1, tzinfo=UTC)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    end = datetime(next_year, next_month, 1, tzinfo=UTC)
    return start, end


def get_preaggregated_monthly_spending(
    session: Session, account_id: UUID, year: int, month: int
) -> list[MonthlyCategorySpending]:
    """Stored per-category spend for an account-month, largest category first."""
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
    """Per-category spend of an account-month computed from the immutable ledger entries.

    Spend is every DEBIT entry of a POSTED transaction that is not itself a REVERSAL:
    a reversed transfer was refunded (its status is REVERSED), so it is no longer spend, and the
    compensating REVERSAL that debits the counterparty is money being returned, not consumption.
    Deposits and incoming transfers are CREDIT entries and never count.
    """
    start, end = month_window(year, month)
    stmt = (
        select(TransactionEntry.amount, Transaction.reference, Transaction.type)
        .join(Transaction, Transaction.id == TransactionEntry.transaction_id)
        .where(
            TransactionEntry.account_id == account_id,
            TransactionEntry.direction == EntryDirection.DEBIT,
            TransactionEntry.created_at >= start,
            TransactionEntry.created_at < end,
            Transaction.status == TransactionStatus.POSTED,
            Transaction.type != TransactionType.REVERSAL,
        )
    )
    totals: dict[str, int] = {}
    counts: dict[str, int] = {}
    for amount, reference, tx_type in session.execute(stmt).all():
        category = categorize_transaction(reference, str(tx_type) if tx_type else None)
        totals[category] = totals.get(category, 0) + int(amount)
        counts[category] = counts.get(category, 0) + 1

    records = [
        MonthlyCategorySpending(
            account_id=account_id,
            year=year,
            month=month,
            category=category,
            total_amount_minor=total,
            transaction_count=counts[category],
        )
        for category, total in totals.items()
    ]
    records.sort(key=lambda r: r.total_amount_minor, reverse=True)
    return records


def accounts_with_debits(session: Session, year: int, month: int) -> list[UUID]:
    """Customer accounts with any debit entry in the month (SYSTEM accounts are not customers)."""
    start, end = month_window(year, month)
    stmt = (
        select(TransactionEntry.account_id)
        .join(Account, Account.id == TransactionEntry.account_id)
        .where(
            Account.account_type != AccountType.SYSTEM,
            TransactionEntry.direction == EntryDirection.DEBIT,
            TransactionEntry.created_at >= start,
            TransactionEntry.created_at < end,
        )
        .distinct()
        .order_by(TransactionEntry.account_id)
    )
    return list(session.execute(stmt).scalars().all())


def upsert_monthly_aggregates(
    session: Session,
    account_id: UUID,
    year: int,
    month: int,
    records: list[MonthlyCategorySpending],
) -> None:
    """Make the stored rows of one account-month equal to ``records``.

    Categories that disappeared (e.g. every transfer in them was reversed) are deleted; the rest
    are inserted or updated in place under the unique key, so concurrent or repeated runs cannot
    collide on it. The caller owns the transaction.
    """
    keep = [record.category for record in records]
    stale = delete(MonthlyCategorySpending).where(
        MonthlyCategorySpending.account_id == account_id,
        MonthlyCategorySpending.year == year,
        MonthlyCategorySpending.month == month,
    )
    if keep:
        stale = stale.where(MonthlyCategorySpending.category.not_in(keep))
    session.execute(stale)
    if not records:
        return
    stmt = insert(MonthlyCategorySpending).values(
        [
            {
                "account_id": account_id,
                "year": year,
                "month": month,
                "category": record.category,
                "total_amount_minor": record.total_amount_minor,
                "transaction_count": record.transaction_count,
            }
            for record in records
        ]
    )
    session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_monthly_category_spending_account_period_cat",
            set_={
                "total_amount_minor": stmt.excluded.total_amount_minor,
                "transaction_count": stmt.excluded.transaction_count,
                "updated_at": func.now(),
            },
        )
    )
