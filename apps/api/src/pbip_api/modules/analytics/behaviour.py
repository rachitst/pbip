"""Behavioural analytics across customers for staff (TASK-101, UC-12 Behavioural Analytics).

Four aggregate panels, each one set-based query over tables the platform already writes. Every
number is a count, sum or average over a group of customers; no customer id, name or account
number leaves this module, so the dashboard cannot be used to look at one person (the RM insights
page is the per-customer view, ADR-0027).

* Segments: the latest weekly assignment in ``customer_segments`` (ADR-0012); a customer the job has
  not assigned yet is ``UNSEGMENTED``. Averages are of the RFM features the job stored.
* Spending in the period: DEBIT ledger entries of POSTED, non-reversal INR transactions on accounts
  where the customer is the PRIMARY holder, leaving out moves between the customer's own accounts
  (that money is not spent). Grouped by segment and by transaction type.
* Product affinity: share of customers holding each product today (an ACTIVE savings or current
  account as holder or joint holder, an ACTIVE loan by type), and ``product_clicked`` clickstream
  events in the period whose ``product_id`` is a catalogue product (recommendation interactions).
* Engagement in the period: clickstream events, sessions (one per customer and session id), active
  customers, events per session, mean session length (last event minus first event of a session,
  so a one-event session counts as 0 s), events by type and active customers per day (UTC).
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from pbip_ml.recommendations import PRODUCT_CATALOGUE
from pbip_ml.segmentation.contract import CustomerSegmentName
from sqlalchemy import Date, String, cast, distinct, exists, func, literal, select, union_all
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.selectable import Subquery

from pbip_api.modules.accounts.models import (
    Account,
    AccountHolder,
    AccountStatus,
    AccountType,
    HolderRole,
)
from pbip_api.modules.analytics.models import CustomerSegment
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.events.models import ClickstreamEvent
from pbip_api.modules.events.schemas import TelemetryEventType
from pbip_api.modules.ledger.models import (
    EntryDirection,
    Transaction,
    TransactionEntry,
    TransactionStatus,
    TransactionType,
)
from pbip_api.modules.loans.models import Loan, LoanStatus

UNSEGMENTED = "UNSEGMENTED"
SEGMENTS: tuple[str, ...] = (*(s.value for s in CustomerSegmentName), UNSEGMENTED)
CURRENCY = "INR"  # every customer account is INR; other currencies would not add up
PRODUCTS: tuple[str, ...] = (
    "SAVINGS_ACCOUNT",
    "CURRENT_ACCOUNT",
    "PERSONAL_LOAN",
    "AUTO_LOAN",
    "HOME_LOAN",
)
_CATALOGUE = {str(p.product_id): p for p in PRODUCT_CATALOGUE}


@dataclass(frozen=True, slots=True)
class Period:
    start: datetime
    end: datetime


def share_pct(part: int, whole: int) -> float:
    """``part`` as a percentage of ``whole`` to one decimal; 0.0 when there is no whole."""
    return round(100.0 * part / whole, 1) if whole else 0.0


def _scope(segment: str | None) -> Subquery:
    """(customer_id, segment) of every customer, narrowed to one segment when filtered."""
    label = func.coalesce(CustomerSegment.segment, literal(UNSEGMENTED, String))
    query = select(Customer.id.label("customer_id"), label.label("segment")).outerjoin(
        CustomerSegment, CustomerSegment.customer_id == Customer.id
    )
    if segment is not None:
        query = query.where(label == segment)
    return query.subquery("scope")


def segments_panel(session: Session) -> dict[str, Any]:
    """Every segment's size and average RFM features; never filtered, so the mix stays visible."""
    scope = _scope(None)
    sizes = dict.fromkeys(SEGMENTS, 0)
    for segment, n in session.execute(
        select(scope.c.segment, func.count()).group_by(scope.c.segment)
    ):
        sizes[str(segment)] = int(n)
    profiles = {
        row[0]: row[1:]
        for row in session.execute(
            select(
                CustomerSegment.segment,
                func.avg(CustomerSegment.recency_days),
                func.avg(CustomerSegment.frequency),
                func.avg(CustomerSegment.monetary_minor),
                func.avg(CustomerSegment.savings_ratio),
                func.avg(CustomerSegment.discretionary_ratio),
            ).group_by(CustomerSegment.segment)
        )
    }
    total = sum(sizes.values())
    items = []
    for segment in SEGMENTS:
        profile = profiles.get(segment)
        items.append(
            {
                "segment": segment,
                "customers": sizes[segment],
                "share_pct": share_pct(sizes[segment], total),
                "avg_recency_days": _round(profile[0], 1) if profile else None,
                "avg_monthly_transactions": _round(profile[1], 2) if profile else None,
                "avg_monthly_spend_minor": round(float(profile[2])) if profile else None,
                "avg_savings_ratio": _round(profile[3], 3) if profile else None,
                "avg_discretionary_ratio": _round(profile[4], 3) if profile else None,
            }
        )
    return {
        "total_customers": total,
        "last_segmented_at": session.scalar(select(func.max(CustomerSegment.updated_at))),
        "items": items,
    }


def _round(value: Any, digits: int) -> float:
    return round(float(value), digits)


def _spend_rows(scope: Subquery, period: Period) -> Subquery:
    """One row per spend entry: (segment, customer_id, type, amount); see the module docstring."""
    holder = aliased(AccountHolder)
    own = aliased(AccountHolder)
    to_own_account = exists().where(
        own.account_id == Transaction.credit_account_id, own.customer_id == holder.customer_id
    )
    return (
        select(
            scope.c.segment,
            scope.c.customer_id,
            Transaction.type.label("tx_type"),
            TransactionEntry.amount,
        )
        .select_from(TransactionEntry)
        .join(Transaction, Transaction.id == TransactionEntry.transaction_id)
        .join(holder, holder.account_id == TransactionEntry.account_id)
        .join(scope, scope.c.customer_id == holder.customer_id)
        .where(
            holder.holder_role == HolderRole.PRIMARY,
            TransactionEntry.direction == EntryDirection.DEBIT,
            TransactionEntry.created_at >= period.start,
            TransactionEntry.created_at < period.end,
            Transaction.status == TransactionStatus.POSTED,
            Transaction.type != TransactionType.REVERSAL,
            Transaction.currency == CURRENCY,
            ~to_own_account,
        )
        .subquery("spend")
    )


def spending_panel(session: Session, scope: Subquery, period: Period) -> list[dict[str, Any]]:
    """Spend per segment in the period, with the mix by transaction type."""
    sizes = {
        str(s): int(n)
        for s, n in session.execute(select(scope.c.segment, func.count()).group_by(scope.c.segment))
    }
    spend = _spend_rows(scope, period)
    totals = {
        str(s): (int(spenders), int(amount), int(n))
        for s, spenders, amount, n in session.execute(
            select(
                spend.c.segment,
                func.count(distinct(spend.c.customer_id)),
                func.sum(spend.c.amount),
                func.count(),
            ).group_by(spend.c.segment)
        )
    }
    mix: dict[str, list[dict[str, Any]]] = {}
    for segment, tx_type, amount, n in session.execute(
        select(spend.c.segment, spend.c.tx_type, func.sum(spend.c.amount), func.count())
        .group_by(spend.c.segment, spend.c.tx_type)
        .order_by(spend.c.segment, func.sum(spend.c.amount).desc(), spend.c.tx_type)
    ):
        mix.setdefault(str(segment), []).append(
            {"transaction_type": str(tx_type), "spend_minor": int(amount), "transactions": int(n)}
        )
    panel = []
    for segment in SEGMENTS:
        if segment not in sizes:
            continue
        spenders, amount, n = totals.get(segment, (0, 0, 0))
        panel.append(
            {
                "segment": segment,
                "customers": sizes[segment],
                "spenders": spenders,
                "spend_minor": amount,
                "transactions": n,
                "avg_spend_per_spender_minor": amount // spenders if spenders else 0,
                "by_type": mix.get(segment, []),
            }
        )
    return panel


def _holdings() -> Subquery:
    """(customer_id, product) for every product a customer holds today."""
    accounts = (
        select(
            AccountHolder.customer_id,
            (cast(Account.account_type, String) + "_ACCOUNT").label("product"),
        )
        .join(Account, Account.id == AccountHolder.account_id)
        .where(Account.status == AccountStatus.ACTIVE, Account.account_type != AccountType.SYSTEM)
    )
    loans = select(Loan.customer_id, cast(Loan.loan_type, String) + "_LOAN").where(
        Loan.status == LoanStatus.ACTIVE
    )
    return union_all(accounts, loans).subquery("holdings")


def affinity_panel(session: Session, scope: Subquery, period: Period) -> dict[str, Any]:
    """Holdings share per segment and product, and recommendation clicks per catalogue product."""
    sizes = {
        str(s): int(n)
        for s, n in session.execute(select(scope.c.segment, func.count()).group_by(scope.c.segment))
    }
    held = _holdings()
    holders: dict[tuple[str, str], int] = {
        (str(s), str(p)): int(n)
        for s, p, n in session.execute(
            select(scope.c.segment, held.c.product, func.count(distinct(held.c.customer_id)))
            .join(scope, scope.c.customer_id == held.c.customer_id)
            .group_by(scope.c.segment, held.c.product)
        )
    }
    segments = [
        {
            "segment": segment,
            "customers": sizes[segment],
            "holdings": [
                {
                    "product": product,
                    "holders": holders.get((segment, product), 0),
                    "share_pct": share_pct(holders.get((segment, product), 0), sizes[segment]),
                }
                for product in PRODUCTS
            ],
        }
        for segment in SEGMENTS
        if segment in sizes
    ]
    return {
        "products": list(PRODUCTS),
        "segments": segments,
        "recommendation_clicks": _clicks(session, scope, period),
    }


def _clicks(session: Session, scope: Subquery, period: Period) -> list[dict[str, Any]]:
    product_id = ClickstreamEvent.properties["product_id"].astext
    rows = session.execute(
        select(product_id, func.count(), func.count(distinct(ClickstreamEvent.customer_id)))
        .join(scope, scope.c.customer_id == ClickstreamEvent.customer_id)
        .where(
            ClickstreamEvent.event_type == TelemetryEventType.PRODUCT_CLICKED.value,
            ClickstreamEvent.event_timestamp >= period.start,
            ClickstreamEvent.event_timestamp < period.end,
            product_id.in_(list(_CATALOGUE)),
        )
        .group_by(product_id)
        .order_by(func.count().desc(), product_id)
    ).all()
    return [
        {
            "product_id": UUID(pid),
            "title": _CATALOGUE[pid].title,
            "product_type": _CATALOGUE[pid].product_type.value,
            "clicks": int(clicks),
            "customers": int(customers),
        }
        for pid, clicks, customers in rows
    ]


def engagement_panel(session: Session, scope: Subquery, period: Period) -> dict[str, Any]:
    """Clickstream engagement of the customers in scope over the period."""
    in_period = (
        ClickstreamEvent.event_timestamp >= period.start,
        ClickstreamEvent.event_timestamp < period.end,
    )
    events = (
        select(
            scope.c.segment,
            ClickstreamEvent.customer_id,
            ClickstreamEvent.session_id,
            ClickstreamEvent.event_type,
            ClickstreamEvent.event_timestamp,
        )
        .join(scope, scope.c.customer_id == ClickstreamEvent.customer_id)
        .where(*in_period)
        .subquery("ev")
    )
    sessions = (
        select(
            func.extract(
                "epoch", func.max(events.c.event_timestamp) - func.min(events.c.event_timestamp)
            ).label("seconds")
        )
        .group_by(events.c.customer_id, events.c.session_id)
        .subquery("sessions")
    )
    n_events, active = session.execute(
        select(func.count(), func.count(distinct(events.c.customer_id)))
    ).one()
    n_sessions, mean_seconds = session.execute(
        select(func.count(), func.avg(sessions.c.seconds))
    ).one()
    customers = int(session.scalar(select(func.count()).select_from(scope)) or 0)
    return {
        "events": int(n_events),
        "sessions": int(n_sessions),
        "active_customers": int(active),
        "customers": customers,
        "active_share_pct": share_pct(int(active), customers),
        "events_per_session": round(int(n_events) / int(n_sessions), 2) if n_sessions else 0.0,
        "avg_session_seconds": round(float(mean_seconds)) if mean_seconds is not None else 0,
        "by_event_type": _by_event_type(session, events),
        "by_segment": _active_by_segment(session, scope, events),
        "daily": _daily(session, events),
    }


def _by_event_type(session: Session, events: Subquery) -> list[dict[str, Any]]:
    counts = dict.fromkeys((t.value for t in TelemetryEventType), 0)
    for event_type, n in session.execute(
        select(events.c.event_type, func.count()).group_by(events.c.event_type)
    ):
        counts[str(event_type)] = int(n)
    return [{"event_type": t, "events": n} for t, n in counts.items()]


def _active_by_segment(session: Session, scope: Subquery, events: Subquery) -> list[dict[str, Any]]:
    sizes = {
        str(s): int(n)
        for s, n in session.execute(select(scope.c.segment, func.count()).group_by(scope.c.segment))
    }
    active = {
        str(s): int(n)
        for s, n in session.execute(
            select(events.c.segment, func.count(distinct(events.c.customer_id))).group_by(
                events.c.segment
            )
        )
    }
    return [
        {
            "segment": segment,
            "customers": sizes[segment],
            "active_customers": active.get(segment, 0),
            "active_share_pct": share_pct(active.get(segment, 0), sizes[segment]),
        }
        for segment in SEGMENTS
        if segment in sizes
    ]


def _daily(session: Session, events: Subquery) -> list[dict[str, Any]]:
    day = cast(func.timezone("UTC", events.c.event_timestamp), Date)
    rows = session.execute(
        select(day, func.count(distinct(events.c.customer_id)), func.count())
        .group_by(day)
        .order_by(day)
    ).all()
    return [
        {
            "day": d if isinstance(d, date) else date.fromisoformat(str(d)),
            "active_customers": int(a),
            "events": int(n),
        }
        for d, a, n in rows
    ]


def behaviour_analytics(
    session: Session, now: datetime, days: int, segment: str | None
) -> dict[str, Any]:
    """All four panels for the last ``days`` days up to ``now``, optionally for one segment."""
    period = Period(now - timedelta(days=days), now)
    scope = _scope(segment)
    return {
        "generated_at": now,
        "period_days": days,
        "period_start": period.start,
        "period_end": period.end,
        "segment": segment,
        "currency": CURRENCY,
        "segments": segments_panel(session),
        "spending": spending_panel(session, scope, period),
        "affinity": affinity_panel(session, scope, period),
        "engagement": engagement_panel(session, scope, period),
    }
