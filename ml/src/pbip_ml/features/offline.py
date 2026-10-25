"""Offline reference implementation of the customer feature contract.

Point-in-time correct by construction: ``compute_customer_features`` discards every event at or
after ``as_of`` before computing anything. It is the oracle the Sprint 2 online pipeline must
match. Pure Python, no I/O, deterministic.
"""

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from uuid import UUID

from pbip_ml.features.contract import CustomerFeatures, TransactionEvent

_IST = timezone(timedelta(hours=5, minutes=30))
_NIGHT_END_HOUR = 6  # 00:00-05:59 IST counts as night


def point_in_time(events: Iterable[TransactionEvent], as_of: datetime) -> list[TransactionEvent]:
    """Only events strictly before ``as_of``: the leakage guard every feature goes through."""
    return [e for e in events if e.occurred_at < as_of]


def _within(
    events: list[TransactionEvent], as_of: datetime, span: timedelta
) -> list[TransactionEvent]:
    start = as_of - span
    return [e for e in events if e.occurred_at >= start]


def compute_customer_features(
    customer_id: UUID, events: Iterable[TransactionEvent], as_of: datetime
) -> CustomerFeatures:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    past = [e for e in point_in_time(events, as_of) if e.customer_id == customer_id]
    window = _within(past, as_of, timedelta(days=30))
    debits = [e for e in window if e.direction == "DEBIT"]
    recent = [e for e in past if e.direction == "DEBIT"]
    amounts = [e.amount_minor for e in debits]
    nights = sum(1 for e in debits if e.occurred_at.astimezone(_IST).hour < _NIGHT_END_HOUR)
    last = max((e.occurred_at for e in past), default=None)
    return CustomerFeatures(
        customer_id=customer_id,
        as_of=as_of,
        avg_amount_30d=round(sum(amounts) / len(amounts)) if amounts else 0,
        max_amount_30d=max(amounts, default=0),
        debit_count_30d=len(debits),
        debit_total_30d=sum(amounts),
        credit_total_30d=sum(e.amount_minor for e in window if e.direction == "CREDIT"),
        transfer_velocity_10m=len(_within(recent, as_of, timedelta(minutes=10))),
        transfer_velocity_24h=len(_within(recent, as_of, timedelta(hours=24))),
        distinct_merchants_30d=len({e.merchant for e in debits}),
        night_debit_ratio_30d=nights / len(debits) if debits else 0.0,
        days_since_last_txn=(as_of - last).total_seconds() / 86_400 if last else None,
    )
