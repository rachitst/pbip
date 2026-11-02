"""Instalment due dates (TASK-047, ADR-0024).

The first EMI falls one calendar month after disbursal; later ones on the same day of the month as
the first, clamped to the month's last day (31 Jan -> 28/29 Feb -> 31 Mar, no drift). These are
the scheduled transfers engine's MONTHLY rules (``payments.recurrence.compute_next_occurrence``)
with the first due date as the anchor, so the engine's upcoming-occurrence projection of the loan's
instruction lands on exactly these dates (unit-tested against it).
"""

import calendar
from datetime import date


def _add_months(anchor_day: int, current: date, months: int) -> date:
    index = current.month - 1 + months
    year, month = current.year + index // 12, index % 12 + 1
    return date(year, month, min(anchor_day, calendar.monthrange(year, month)[1]))


def due_dates(disbursed_on: date, tenure_months: int) -> list[date]:
    if tenure_months <= 0:
        raise ValueError("tenure must be positive")
    first = _add_months(disbursed_on.day, disbursed_on, 1)
    dates = [first]
    for _ in range(tenure_months - 1):
        dates.append(_add_months(first.day, dates[-1], 1))
    return dates
