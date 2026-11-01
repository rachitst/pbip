"""UTC date recurrence and upcoming occurrence projection (ADR-0021)."""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from pbip_api.modules.payments.models import (
    ScheduledTransfer,
    ScheduledTransferStatus,
    ScheduleFrequency,
)


@dataclass(frozen=True, slots=True)
class UpcomingOccurrence:
    """Projected future occurrence of an active scheduled transfer."""

    scheduled_transfer_id: UUID
    from_account_id: UUID
    amount_minor: int
    currency: str
    occurrence_date: date


def compute_next_occurrence(
    start_date: date,
    current_date: date,
    frequency: ScheduleFrequency | str,
    end_date: date | None = None,
) -> date | None:
    """Compute the next occurrence date anchored to start_date, or None if past end_date.

    Rules:
    - DAILY: +1 calendar day.
    - WEEKLY: +7 calendar days.
    - MONTHLY: advance one calendar month while anchoring to start_date.day.
      Clamp invalid month days to target month's last day (e.g. Jan 31 -> Feb 28/29 -> Mar 31).
      Avoid cumulative month-end drift.
    """
    if current_date < start_date:
        raise ValueError(
            f"current_date ({current_date.isoformat()}) "
            f"cannot be before start_date ({start_date.isoformat()})"
        )

    freq_str = str(frequency)
    if freq_str == ScheduleFrequency.DAILY or freq_str == "DAILY":
        candidate = current_date + timedelta(days=1)
    elif freq_str == ScheduleFrequency.WEEKLY or freq_str == "WEEKLY":
        candidate = current_date + timedelta(days=7)
    elif freq_str == ScheduleFrequency.MONTHLY or freq_str == "MONTHLY":
        year = current_date.year + (1 if current_date.month == 12 else 0)
        month = 1 if current_date.month == 12 else current_date.month + 1
        max_day = calendar.monthrange(year, month)[1]
        day = min(start_date.day, max_day)
        candidate = date(year, month, day)
    else:
        raise ValueError(f"unsupported recurrence frequency: {frequency!r}")

    if end_date is not None and candidate > end_date:
        return None
    return candidate


def compute_missed_occurrences(
    start_date: date,
    next_run_date: date,
    frequency: ScheduleFrequency | str,
    today_utc: date,
    end_date: date | None = None,
) -> list[date]:
    """Return all expired occurrence dates strictly prior to today_utc."""
    if next_run_date >= today_utc:
        return []

    missed: list[date] = []
    cur: date | None = next_run_date

    while cur is not None and cur < today_utc:
        if end_date is not None and cur > end_date:
            break
        missed.append(cur)
        cur = compute_next_occurrence(start_date, cur, frequency, end_date)

    return missed


def upcoming_occurrences(
    session: Session, customer_id: UUID, until: date
) -> list[UpcomingOccurrence]:
    """Return projected upcoming occurrences up to until for customer's ACTIVE instructions.

    Read-only: acquires no locks, performs no writes, executes no payments.
    """
    stmt = (
        select(ScheduledTransfer)
        .where(
            ScheduledTransfer.customer_id == customer_id,
            ScheduledTransfer.status == ScheduledTransferStatus.ACTIVE,
            ScheduledTransfer.next_run_date <= until,
        )
        .order_by(ScheduledTransfer.next_run_date.asc(), ScheduledTransfer.id.asc())
    )
    instructions = session.execute(stmt).scalars().all()

    projections: list[UpcomingOccurrence] = []
    for inst in instructions:
        cur: date | None = inst.next_run_date
        while cur is not None and cur <= until:
            if inst.end_date is not None and cur > inst.end_date:
                break
            projections.append(
                UpcomingOccurrence(
                    scheduled_transfer_id=inst.id,
                    from_account_id=inst.from_account_id,
                    amount_minor=inst.amount,
                    currency=inst.currency,
                    occurrence_date=cur,
                )
            )
            cur = compute_next_occurrence(inst.start_date, cur, inst.frequency, inst.end_date)

    projections.sort(key=lambda o: (o.occurrence_date, o.scheduled_transfer_id))
    return projections
