"""End-of-month balance projection (TASK-043): known obligations plus the forecast of the rest.

Pure functions, no I/O, so the arithmetic is unit-tested on its own.

The balance is split in two parts:

* **Known obligations**: the scheduled-transfer engine's debits (standing orders and loan EMIs).
  The upcoming ones are known exactly (date and amount), so they are subtracted as they are.
* **Everything else** (salary, card spend, ad-hoc transfers): forecast by the TASK-042 model from
  the balance history with the scheduled debits added back. Without that step the model would see
  last month's EMI as a pattern in the history *and* we would subtract this month's EMI, counting
  it twice.

``projected(t) = model(t) - scheduled_paid_in_history - obligations_due_from_today_to(t)``

The model forecasts the "balance without scheduled debits", which sits ``scheduled_paid_in_history``
above the real balance at the last history day; subtracting that puts the forecast back on the real
balance. The interval bounds move by the same known amounts: obligations add no uncertainty.
"""

import calendar
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from uuid import UUID

# "Approaches zero or the overdraft limit": the alert fires when the predicted month-end balance is
# within this buffer of the lowest balance the account may reach (0, or minus its overdraft limit).
LOW_BALANCE_BUFFER_MINOR = 1_000_00  # INR 1,000.00


class ObligationKind(StrEnum):
    SCHEDULED_TRANSFER = "SCHEDULED_TRANSFER"
    LOAN_EMI = "LOAN_EMI"


@dataclass(frozen=True, slots=True)
class Obligation:
    """A known upcoming debit of the account: one occurrence of an ACTIVE instruction."""

    date: date
    amount_minor: int
    kind: ObligationKind
    scheduled_transfer_id: UUID
    loan_id: UUID | None
    reference: str | None


@dataclass(frozen=True, slots=True)
class ModelDay:
    """One day of the TASK-042 model output (floats from statsforecast, not money yet)."""

    median: float
    lower_80: float
    upper_80: float
    lower_95: float
    upper_95: float


@dataclass(frozen=True, slots=True)
class ProjectedDay:
    """One projected end-of-day balance, integer minor units. Predicted, not exact."""

    date: date
    predicted_minor: int
    lower_80_minor: int
    upper_80_minor: int
    lower_95_minor: int
    upper_95_minor: int
    obligations_minor: int  # known scheduled debits due on this day (already in the figures)


def month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def discretionary_history(
    history: Sequence[int], first_day: date, scheduled_debits: Mapping[date, int]
) -> tuple[list[int], int]:
    """The balance history with every scheduled debit added back from its day on.

    ``history[i]`` is the end-of-day balance of ``first_day + i``. Returns the adjusted series and
    the total added back by the last day (the gap between the adjusted and the real balance).
    """
    adjusted: list[int] = []
    added_back = 0
    for i, balance in enumerate(history):
        added_back += scheduled_debits.get(first_day + timedelta(days=i), 0)
        adjusted.append(balance + added_back)
    return adjusted, added_back


def project(
    model_days: Sequence[ModelDay],
    first_day: date,
    scheduled_paid_in_history: int,
    obligations: Sequence[Obligation],
) -> list[ProjectedDay]:
    """Combine the model's forecast of the discretionary balance with the known obligations.

    ``model_days[i]`` is the forecast for ``first_day + i``. Obligations are cumulative: an EMI due
    on the 5th lowers every day from the 5th on. Obligations dated before ``first_day`` must already
    have been moved to it (overdue loan instalments) or dropped (missed transfers) by the caller.
    """
    due: dict[date, int] = {}
    for obligation in obligations:
        due[obligation.date] = due.get(obligation.date, 0) + obligation.amount_minor
    days: list[ProjectedDay] = []
    paid = scheduled_paid_in_history
    for i, m in enumerate(model_days):
        day = first_day + timedelta(days=i)
        today_due = due.get(day, 0)
        paid += today_due
        days.append(
            ProjectedDay(
                date=day,
                predicted_minor=round(m.median) - paid,
                lower_80_minor=round(m.lower_80) - paid,
                upper_80_minor=round(m.upper_80) - paid,
                lower_95_minor=round(m.lower_95) - paid,
                upper_95_minor=round(m.upper_95) - paid,
                obligations_minor=today_due,
            )
        )
    return days


def low_balance_threshold(overdraft_limit_minor: int) -> int:
    """The month-end balance at or below which the customer is warned."""
    return LOW_BALANCE_BUFFER_MINOR - overdraft_limit_minor


def is_low(projected: ProjectedDay, threshold_minor: int) -> bool:
    """Uses the central estimate: the alert says what is likely, not the worst case."""
    return projected.predicted_minor <= threshold_minor
