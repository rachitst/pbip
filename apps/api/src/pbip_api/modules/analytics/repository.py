"""Repository for analytics: the materialised monthly table and the ledger entries it is built from.

The ledger is read through its models only (no ledger internals are called) and never written;
the only table this module writes is ``monthly_category_spending``.
"""

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import groupby
from operator import itemgetter
from typing import Any
from uuid import UUID

from pbip_ml.segmentation.contract import CustomerRFMFeatures, SegmentAssignment
from sqlalchemy import ColumnElement, case, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from pbip_api.modules.accounts.models import Account, AccountHolder, AccountType
from pbip_api.modules.analytics.categorization import categorize_transaction, is_debt_repayment
from pbip_api.modules.analytics.models import CustomerSegment, MonthlyCategorySpending
from pbip_api.modules.analytics.unusual import Debit
from pbip_api.modules.auth.models import User
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.events.models import ClickstreamEvent
from pbip_api.modules.ledger.models import (
    EntryDirection,
    Transaction,
    TransactionEntry,
    TransactionStatus,
    TransactionType,
)

HEALTH_WINDOW_MONTHS = 12
INCOME_TYPES = (TransactionType.DEPOSIT, TransactionType.TRANSFER)
# Never income or spend. A REVERSAL returns money; a fixed deposit's booking and principal payout
# move the customer's own money to and from their deposit (ADR-0025), like a move between own
# accounts. Deposit interest (FD_INTEREST) stays visible as a credit.
NOT_ACTIVITY_TYPES = (
    TransactionType.REVERSAL,
    TransactionType.FD_BOOKING,
    TransactionType.FD_CLOSURE,
)
# Income is recurring inflow (ADR-0043): a source of credits (the paying account and the payment
# reference without its dates and numbers) counts only when it paid in at least
# INCOME_MIN_RECURRING_MONTHS distinct calendar months among the running month and the
# INCOME_RECURRENCE_MONTHS - 1 before it. A one-off credit, such as an opening deposit, is not
# income.
INCOME_RECURRENCE_MONTHS = 3
INCOME_MIN_RECURRING_MONTHS = 2
_MONTH_WORDS = re.compile(
    r"\b(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sept?(ember)?"
    r"|oct(ober)?|nov(ember)?|dec(ember)?)\b"
)
_NON_LETTERS = re.compile(r"[^a-z]+")
SCHEDULED_CHANNEL = "SCHEDULED"  # transactions.channel of the scheduled-transfer engine (ADR-0023)


@dataclass(frozen=True)
class HealthMetrics:
    """Ledger facts behind the financial health score, over the trailing window (minor units).

    Transfers between the customer's own accounts are neither income nor spend: money moved
    between pockets was not earned or consumed.
    """

    income_minor: int  # CREDIT entries of POSTED deposits and transfers from other parties,
    # from recurring sources only (``recurring_income_minor``; ADR-0043)
    spend_minor: int  # DEBIT entries of POSTED non-REVERSAL transactions to other parties
    closed_monthly_spend_minor: list[int]  # every closed calendar month since the first entry,
    # oldest first, zero-filled; the running month is excluded (it is partial)
    running_month_spend_minor: int
    debt_repayments_minor: int  # the part of spend whose reference marks a loan or EMI instalment
    months_observed: int  # calendar months from the first entry in the window to now; 0 if none


def window_start(now: datetime, months: int) -> datetime:
    """First instant of the calendar month ``months - 1`` months before ``now`` (UTC)."""
    index = now.year * 12 + (now.month - 1) - (months - 1)
    return datetime(index // 12, index % 12 + 1, 1, tzinfo=UTC)


def _closed_months(first: datetime, now: datetime) -> list[tuple[int, int]]:
    """Calendar months from ``first``'s month up to, but excluding, ``now``'s month."""
    months: list[tuple[int, int]] = []
    year, month = first.year, first.month
    while (year, month) < (now.year, now.month):
        months.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def income_source_key(
    payer_account_id: UUID | None, reference: str | None
) -> tuple[UUID | None, str]:
    """Who paid and what for, with the month names and numbers that change every month removed,
    so "Salary Oct 2026" and "Salary November 2026" from one payer are the same source."""
    text = _MONTH_WORDS.sub(" ", (reference or "").lower())
    return payer_account_id, " ".join(_NON_LETTERS.sub(" ", text).split())


def recurring_income_minor(
    credits: Iterable[tuple[tuple[UUID | None, str], datetime, int]], now: datetime
) -> int:
    """Sum of the income-candidate credits whose source recurs (see INCOME_RECURRENCE_MONTHS).

    ``credits`` are ``(source key, created_at, amount)``. Every credit of a recurring source in
    the window counts; a source seen in only one recent month (an opening deposit, a one-off gift)
    counts for nothing, so loans, the savings rate and the assistant are not sized from it.
    """
    current = now.year * 12 + now.month - 1
    recent: dict[tuple[UUID | None, str], set[int]] = {}
    totals: dict[tuple[UUID | None, str], int] = {}
    for key, created_at, amount in credits:
        stamp = created_at.astimezone(UTC)
        index = stamp.year * 12 + stamp.month - 1
        totals[key] = totals.get(key, 0) + amount
        if 0 <= current - index < INCOME_RECURRENCE_MONTHS:
            recent.setdefault(key, set()).add(index)
    return sum(
        total
        for key, total in totals.items()
        if len(recent.get(key, ())) >= INCOME_MIN_RECURRING_MONTHS
    )


def owned_account_ids(session: Session, customer_id: UUID) -> set[UUID]:
    rows = session.execute(
        select(AccountHolder.account_id).where(AccountHolder.customer_id == customer_id)
    ).scalars()
    return set(rows)


def get_financial_health_metrics(
    session: Session, account_id: UUID, owned: set[UUID], now: datetime
) -> HealthMetrics:
    """One pass over the account's entries in the window (which bounds the work per request)."""
    stmt = (
        select(*_rfm_entry_columns())
        .join(Transaction, Transaction.id == TransactionEntry.transaction_id)
        .where(
            TransactionEntry.account_id == account_id,
            TransactionEntry.created_at >= window_start(now, HEALTH_WINDOW_MONTHS),
            *_rfm_entry_filters(),
        )
        .order_by(TransactionEntry.created_at)
    )
    return _health_from_rows(session.execute(stmt).all(), owned, now)


def _health_from_rows(
    rows: Iterable[Sequence[Any]],  # Any: the columns of _rfm_entry_columns, oldest first
    owned: set[UUID],
    now: datetime,
) -> HealthMetrics:
    spend = debt = 0
    credits: list[tuple[tuple[UUID | None, str], datetime, int]] = []
    by_month: dict[tuple[int, int], int] = {}
    first: datetime | None = None
    for amount, direction, created_at, reference, tx_type, debit_acc, credit_acc in rows:
        first = first or created_at
        if direction == EntryDirection.CREDIT:
            if tx_type in INCOME_TYPES and debit_acc not in owned:
                credits.append((income_source_key(debit_acc, reference), created_at, int(amount)))
            continue
        if credit_acc in owned:
            continue  # moved to another of the customer's own accounts
        spend += int(amount)
        stamp = created_at.astimezone(UTC)
        by_month[(stamp.year, stamp.month)] = by_month.get((stamp.year, stamp.month), 0) + int(
            amount
        )
        if is_debt_repayment(reference):
            debt += int(amount)
    income = recurring_income_minor(credits, now)
    if first is None:
        return HealthMetrics(income, spend, [], 0, debt, 0)
    begin = first.astimezone(UTC)
    closed = [by_month.get(key, 0) for key in _closed_months(begin, now)]
    return HealthMetrics(
        income_minor=income,
        spend_minor=spend,
        closed_monthly_spend_minor=closed,
        running_month_spend_minor=by_month.get((now.year, now.month), 0),
        debt_repayments_minor=debt,
        months_observed=(now.year - begin.year) * 12 + (now.month - begin.month) + 1,
    )


# Arbitrary, project-unique key for pg advisory locks taken by the spending materialisation.
MATERIALIZATION_LOCK_KEY = 4_040_001


def month_window(year: int, month: int) -> tuple[datetime, datetime]:
    """[start, end) of a calendar month in UTC; ledger timestamps are UTC."""
    start = datetime(year, month, 1, tzinfo=UTC)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    end = datetime(next_year, next_month, 1, tzinfo=UTC)
    return start, end


@dataclass(frozen=True, slots=True)
class CategorySpend:
    """One category's spend in an account-month: a plain value, never an ORM object.

    The read path computes these from the ledger for the running month; returning detached
    ``MonthlyCategorySpending`` instances instead meant a later ``session.add`` of one would turn
    a GET into a write (#175).
    """

    category: str
    total_amount_minor: int
    transaction_count: int


def get_preaggregated_monthly_spending(
    session: Session, account_id: UUID, year: int, month: int
) -> list[CategorySpend]:
    """Stored per-category spend for an account-month, largest category first."""
    stmt = (
        select(
            MonthlyCategorySpending.category,
            MonthlyCategorySpending.total_amount_minor,
            MonthlyCategorySpending.transaction_count,
        )
        .where(
            MonthlyCategorySpending.account_id == account_id,
            MonthlyCategorySpending.year == year,
            MonthlyCategorySpending.month == month,
        )
        .order_by(MonthlyCategorySpending.total_amount_minor.desc())
    )
    return [
        CategorySpend(category, int(total), int(count))
        for category, total, count in session.execute(stmt).all()
    ]


def compute_monthly_spending_from_entries(
    session: Session, account_id: UUID, year: int, month: int
) -> list[CategorySpend]:
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
            Transaction.type.not_in(NOT_ACTIVITY_TYPES),
        )
    )
    totals: dict[str, int] = {}
    counts: dict[str, int] = {}
    for amount, reference, tx_type in session.execute(stmt).all():
        category = categorize_transaction(reference, str(tx_type))
        totals[category] = totals.get(category, 0) + int(amount)
        counts[category] = counts.get(category, 0) + 1

    records = [
        CategorySpend(category, total, counts[category]) for category, total in totals.items()
    ]
    records.sort(key=lambda r: r.total_amount_minor, reverse=True)
    return records


def spend_debits(
    session: Session, account_id: UUID, owned: set[UUID], start: datetime, end: datetime
) -> list[Debit]:
    """Spend debits of an account in ``[start, end)``, oldest first (unusual expenses, TASK-110).

    Same definition of spend as the monthly breakdown (POSTED, not a REVERSAL), minus moves to the
    customer's own accounts: moving money between pockets is not an expense. Uses
    ``ix_transaction_entries_account_id_created_at``.
    """
    stmt = (
        select(
            TransactionEntry.amount,
            TransactionEntry.created_at,
            Transaction.reference,
            Transaction.type,
            Transaction.credit_account_id,
        )
        .join(Transaction, Transaction.id == TransactionEntry.transaction_id)
        .where(
            TransactionEntry.account_id == account_id,
            TransactionEntry.direction == EntryDirection.DEBIT,
            TransactionEntry.created_at >= start,
            TransactionEntry.created_at < end,
            Transaction.status == TransactionStatus.POSTED,
            Transaction.type != TransactionType.REVERSAL,
        )
        .order_by(TransactionEntry.created_at, TransactionEntry.seq)
    )
    return [
        Debit(
            amount_minor=int(amount),
            category=categorize_transaction(reference, str(tx_type)),
            reference=reference,
            posted_at=created_at.astimezone(UTC),
        )
        for amount, created_at, reference, tx_type, credit_account in session.execute(stmt).all()
        if credit_account not in owned
    ]


def try_lock_materialization(session: Session) -> bool:
    """Transaction-scoped advisory lock so two materialisation runs never interleave (released at
    commit/rollback). Returns False when another run holds it."""
    return bool(
        session.execute(
            select(func.pg_try_advisory_xact_lock(MATERIALIZATION_LOCK_KEY))
        ).scalar_one()
    )


def account_months_reversed_since(session: Session, since: datetime) -> list[tuple[UUID, int, int]]:
    """(account, year, month) of the original debit of every transfer reversed since ``since``.

    A reversal flips the original transaction to REVERSED without adding an entry to the month it
    was posted in, so stored rows for that month would otherwise never be refreshed.
    """
    reversal = aliased(Transaction)
    stmt = (
        select(TransactionEntry.account_id, TransactionEntry.created_at)
        .join(Transaction, Transaction.id == TransactionEntry.transaction_id)
        .join(reversal, reversal.reversal_of_id == Transaction.id)
        .where(
            Transaction.status == TransactionStatus.REVERSED,
            TransactionEntry.direction == EntryDirection.DEBIT,
            reversal.created_at >= since,
        )
    )
    seen: set[tuple[UUID, int, int]] = set()
    for account_id, created_at in session.execute(stmt).all():
        stamp = created_at.astimezone(UTC)
        seen.add((account_id, stamp.year, stamp.month))
    return sorted(seen, key=lambda item: (str(item[0]), item[1], item[2]))


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
    records: list[CategorySpend],
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
            for record in sorted(records, key=lambda r: r.category)  # stable lock order
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


DISCRETIONARY_CATEGORIES = frozenset({"dining", "shopping", "entertainment", "travel"})
DAYS_PER_MONTH = 30.0
RFM_STREAM_BATCH = 10_000  # rows fetched per round trip by the segmentation pass


def _rfm_entry_columns() -> tuple[Any, ...]:
    # Any: a heterogeneous tuple of mapped columns (amount, direction, timestamp, text, ids).
    return (
        TransactionEntry.amount,
        TransactionEntry.direction,
        TransactionEntry.created_at,
        Transaction.reference,
        Transaction.type,
        Transaction.debit_account_id,
        Transaction.credit_account_id,
    )


def _rfm_entry_filters() -> tuple[ColumnElement[bool], ...]:
    return (
        Transaction.status == TransactionStatus.POSTED,
        Transaction.type.not_in(NOT_ACTIVITY_TYPES),
    )


def _rfm_from_rows(
    customer_id: UUID,
    rows: Iterable[Sequence[Any]],  # Any: the columns of _rfm_entry_columns
    owned: set[UUID],
    as_of: datetime,
) -> CustomerRFMFeatures | None:
    """Aggregate one customer's entries (oldest first) into RFM features; None when none count.

    Transfers between two of the customer's own accounts are skipped: moving money between pockets
    is neither spending nor income (the same rule as the health score).
    """
    credits = debits = debit_count = discretionary = 0
    first: datetime | None = None
    last: datetime | None = None
    for amount, direction, created_at, reference, tx_type, debit_account, credit_account in rows:
        if debit_account in owned and credit_account in owned:
            continue
        first = first or created_at
        last = created_at
        if direction == EntryDirection.CREDIT:
            credits += int(amount)
            continue
        debits += int(amount)
        debit_count += 1
        if categorize_transaction(reference, str(tx_type)) in DISCRETIONARY_CATEGORIES:
            discretionary += int(amount)
    if first is None or last is None:
        return None
    months = max((as_of - first).days / DAYS_PER_MONTH, 1.0)
    return CustomerRFMFeatures(
        customer_id=customer_id,
        recency_days=max((as_of - last).days, 0),
        frequency=round(debit_count / months, 2),
        monetary_minor=round(debits / months),
        savings_ratio=round((credits - debits) / credits, 4) if credits > 0 else 0.0,
        discretionary_ratio=round(discretionary / debits, 4) if debits > 0 else 0.0,
    )


def extract_customer_rfm_features(
    session: Session,
    customer_id: UUID,
    as_of: datetime,
    account_ids: set[UUID] | None = None,
) -> CustomerRFMFeatures | None:
    """RFM and lifestyle features of a customer from POSTED, non-REVERSAL entries on their accounts.

    Returns None when the customer has no remaining entries: there is nothing to cluster, and a
    made-up "dormant" profile would not be a measurement.

    ``account_ids`` are the customer's accounts when the caller has just read them (same query as
    ``owned_account_ids``, #343); otherwise they are read here.
    """
    owned = owned_account_ids(session, customer_id) if account_ids is None else account_ids
    if not owned:
        return None
    stmt = (
        select(*_rfm_entry_columns())
        .join(Transaction, Transaction.id == TransactionEntry.transaction_id)
        .where(TransactionEntry.account_id.in_(owned), *_rfm_entry_filters())
        .order_by(TransactionEntry.created_at)
    )
    return _rfm_from_rows(customer_id, session.execute(stmt).all(), owned, as_of)


def health_and_rfm_of_customer(
    session: Session, customer_id: UUID, account_id: UUID, owned: set[UUID], now: datetime
) -> tuple[HealthMetrics, CustomerRFMFeatures | None]:
    """``get_financial_health_metrics`` of ``account_id`` and ``extract_customer_rfm_features`` of
    the customer from ONE query (#343): the account's window is a subset of the entries the RFM
    features read (same filters, every owned account, all history), so it is taken from those rows
    in Python instead of being read again. Both aggregations are order-independent sums, minimums
    and maximums, so the results are exactly those of the two separate functions.
    """
    stmt = (
        select(TransactionEntry.account_id, *_rfm_entry_columns())
        .join(Transaction, Transaction.id == TransactionEntry.transaction_id)
        .where(TransactionEntry.account_id.in_(owned), *_rfm_entry_filters())
        .order_by(TransactionEntry.created_at)
    )
    rows = session.execute(stmt).all()
    start = window_start(now, HEALTH_WINDOW_MONTHS)
    window = (row[1:] for row in rows if row[0] == account_id and row[3] >= start)
    health = _health_from_rows(window, owned, now)
    return health, _rfm_from_rows(customer_id, (row[1:] for row in rows), owned, now)


def extract_all_customers_rfm_features(
    session: Session, as_of: datetime
) -> list[CustomerRFMFeatures]:
    """Features of every customer that has ledger activity, ordered by customer id.

    Two queries per run, not two per customer (#175): the holdings, then every counted entry of
    every held account in one pass ordered by holder, aggregated per customer while streaming. A
    joint account's entries count once for each holder, exactly as in the per-customer function.
    """
    owned_by: defaultdict[UUID, set[UUID]] = defaultdict(set)
    for customer_id, account_id in session.execute(
        select(AccountHolder.customer_id, AccountHolder.account_id)
    ).all():
        owned_by[customer_id].add(account_id)
    stmt = (
        select(AccountHolder.customer_id, *_rfm_entry_columns())
        .select_from(AccountHolder)
        .join(TransactionEntry, TransactionEntry.account_id == AccountHolder.account_id)
        .join(Transaction, Transaction.id == TransactionEntry.transaction_id)
        .where(*_rfm_entry_filters())
        .order_by(AccountHolder.customer_id, TransactionEntry.created_at)
        .execution_options(yield_per=RFM_STREAM_BATCH)
    )
    features: list[CustomerRFMFeatures] = []
    for customer_id, group in groupby(session.execute(stmt), key=itemgetter(0)):
        entries = (row[1:] for row in group)
        rfm = _rfm_from_rows(customer_id, entries, owned_by[customer_id], as_of)
        if rfm is not None:
            features.append(rfm)
    return features


def get_customer_segment(session: Session, customer_id: UUID) -> CustomerSegment | None:
    return session.execute(
        select(CustomerSegment).where(CustomerSegment.customer_id == customer_id)
    ).scalar_one_or_none()


def upsert_customer_segment(
    session: Session, assignment: SegmentAssignment, rfm: CustomerRFMFeatures
) -> None:
    """Insert or replace the customer's segment row under its unique key (safe to re-run)."""
    values = {
        "customer_id": assignment.customer_id,
        "segment": assignment.segment.value,
        "confidence_score": assignment.confidence_score,
        "recency_days": rfm.recency_days,
        "frequency": rfm.frequency,
        "monetary_minor": rfm.monetary_minor,
        "savings_ratio": rfm.savings_ratio,
        "discretionary_ratio": rfm.discretionary_ratio,
        "explanation": assignment.explanation,
        "updated_at": assignment.assigned_at,
    }
    stmt = insert(CustomerSegment).values(values)
    session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_customer_segments_customer_id",
            set_={key: getattr(stmt.excluded, key) for key in values if key != "customer_id"},
        )
    )


@dataclass(frozen=True)
class CustomerActivity:
    """Ledger and activity facts of one customer as of a point in time (minor units)."""

    customer_id: UUID
    full_name: str
    segment: str | None
    created_at: datetime
    balance_minor: int  # posted entries on the customer's accounts up to ``as_of``
    balance_window_start_minor: int  # the same, up to ``as_of - window``
    last_activity_at: datetime | None  # latest login, ledger entry or clickstream event


def customer_activity(
    session: Session, as_of: datetime, window: timedelta, customer_id: UUID | None = None
) -> list[CustomerActivity]:
    """Every customer's balances and last activity in ONE query (no per-customer round trips).

    ``customer_id`` narrows it to one customer (the RM detail view) with the same arithmetic.

    Balances are summed from the immutable entries rather than read from ``accounts`` so that they
    can be taken at any point in time; transfers between a customer's own accounts net to zero.
    """
    start = as_of - window
    signed = case(
        (TransactionEntry.direction == EntryDirection.CREDIT, TransactionEntry.amount),
        else_=-TransactionEntry.amount,
    )
    ledger = (
        select(
            AccountHolder.customer_id.label("customer_id"),
            func.coalesce(func.sum(signed), 0).label("balance"),
            func.coalesce(
                func.sum(case((TransactionEntry.created_at <= start, signed), else_=0)), 0
            ).label("balance_start"),
            func.max(TransactionEntry.created_at).label("last_entry"),
        )
        .join(TransactionEntry, TransactionEntry.account_id == AccountHolder.account_id)
        .where(TransactionEntry.created_at <= as_of)
        .group_by(AccountHolder.customer_id)
        .subquery()
    )
    events = (
        select(
            ClickstreamEvent.customer_id.label("customer_id"),
            func.max(ClickstreamEvent.event_timestamp).label("last_event"),
        )
        .where(ClickstreamEvent.event_timestamp <= as_of)
        .group_by(ClickstreamEvent.customer_id)
        .subquery()
    )
    query = (
        select(
            Customer.id,
            Customer.full_name,
            # The offline job writes ``customer_segments`` (ADR-0012); ``customers.segment`` is the
            # older column nothing assigns, kept only as a fallback.
            func.coalesce(CustomerSegment.segment, Customer.segment).label("segment"),
            Customer.created_at,
            User.last_login_at,
            ledger.c.balance,
            ledger.c.balance_start,
            ledger.c.last_entry,
            events.c.last_event,
        )
        .join(User, User.id == Customer.user_id)
        .outerjoin(CustomerSegment, CustomerSegment.customer_id == Customer.id)
        .outerjoin(ledger, ledger.c.customer_id == Customer.id)
        .outerjoin(events, events.c.customer_id == Customer.id)
        .order_by(Customer.id)
    )
    if customer_id is not None:
        query = query.where(Customer.id == customer_id)
    rows = session.execute(query).all()
    return [
        CustomerActivity(
            customer_id=row.id,
            full_name=row.full_name,
            segment=row.segment,
            created_at=row.created_at,
            balance_minor=int(row.balance or 0),
            balance_window_start_minor=int(row.balance_start or 0),
            last_activity_at=max(
                (
                    ts
                    for ts in (row.last_login_at, row.last_entry, row.last_event)
                    if ts is not None and ts <= as_of
                ),
                default=None,
            ),
        )
        for row in rows
    ]


@dataclass(frozen=True)
class TransactionVolume:
    """Posted money movements with other parties over a window (minor units)."""

    transaction_count: int
    inflow_minor: int
    outflow_minor: int


def transaction_volume(
    session: Session, customer_id: UUID, start: datetime, end: datetime
) -> TransactionVolume:
    """Counts each posted transaction once; moves between the customer's own accounts are skipped.

    Reversals are left out, as in the health metrics, so a corrected mistake is not activity.
    """
    owned = select(AccountHolder.account_id).where(AccountHolder.customer_id == customer_id)
    # ``IS NOT NULL`` first: a deposit has no debit account, and NULL IN (...) would be NULL.
    debit_owned = Transaction.debit_account_id.is_not(None) & Transaction.debit_account_id.in_(
        owned
    )
    credit_owned = Transaction.credit_account_id.is_not(None) & Transaction.credit_account_id.in_(
        owned
    )
    inflow = credit_owned & ~debit_owned
    outflow = debit_owned & ~credit_owned
    row = session.execute(
        select(
            func.count().filter(inflow | outflow),
            func.coalesce(func.sum(Transaction.amount).filter(inflow), 0),
            func.coalesce(func.sum(Transaction.amount).filter(outflow), 0),
        ).where(
            debit_owned | credit_owned,
            Transaction.status == TransactionStatus.POSTED,
            Transaction.type.not_in(NOT_ACTIVITY_TYPES),
            Transaction.created_at >= start,
            Transaction.created_at <= end,
        )
    ).one()
    return TransactionVolume(int(row[0]), int(row[1]), int(row[2]))


def daily_balances(session: Session, account_id: UUID, now: datetime, days: int) -> list[int]:
    """End-of-day ledger balance for each UTC day in the last ``days`` days that the account has
    existed (from its first entry), forward-filled over days without entries; today is excluded."""
    start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    opening = session.execute(
        select(TransactionEntry.balance_after)
        .where(TransactionEntry.account_id == account_id, TransactionEntry.created_at < start)
        .order_by(TransactionEntry.created_at.desc(), TransactionEntry.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    rows = session.execute(
        select(TransactionEntry.created_at, TransactionEntry.balance_after)
        .where(
            TransactionEntry.account_id == account_id,
            TransactionEntry.created_at >= start,
            TransactionEntry.created_at < today,
        )
        .order_by(TransactionEntry.created_at, TransactionEntry.id)
    ).all()
    end_of_day: dict[datetime, int] = {}
    for created_at, balance in rows:
        end_of_day[
            created_at.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        ] = int(balance)
    if opening is None and not end_of_day:
        return []
    day = start if opening is not None else min(end_of_day)
    last = int(opening) if opening is not None else 0
    series = []
    while day < today:
        last = end_of_day.get(day, last)
        series.append(last)
        day += timedelta(days=1)
    return series


def scheduled_debits_by_day(
    session: Session, account_id: UUID, now: datetime, days: int
) -> dict[date, int]:
    """Debits the scheduled-transfer engine posted on the account per UTC day, over the same window
    as ``daily_balances`` (today excluded).

    These are the known obligations already paid (standing orders and loan EMIs, ``channel =
    'SCHEDULED'``). A reversed transfer is left out: its debit and the compensating credit are
    both in the balance, so they already cancel there.
    """
    start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day = func.date(func.timezone("UTC", TransactionEntry.created_at))
    rows = session.execute(
        select(day, func.sum(TransactionEntry.amount))
        .join(Transaction, Transaction.id == TransactionEntry.transaction_id)
        .where(
            TransactionEntry.account_id == account_id,
            TransactionEntry.direction == EntryDirection.DEBIT,
            TransactionEntry.created_at >= start,
            TransactionEntry.created_at < today,
            Transaction.channel == SCHEDULED_CHANNEL,
            Transaction.status != TransactionStatus.REVERSED,
        )
        .group_by(day)
    ).all()
    return {row[0]: int(row[1]) for row in rows}
