"""Loan instalment due dates (TASK-047, ADR-0024): month-end clamping without drift, and the same
dates the scheduled transfers engine projects for the loan's MONTHLY instruction."""

from datetime import date, timedelta
from itertools import pairwise

import pytest
from pbip_api.modules.loans.due_dates import due_dates
from pbip_api.modules.payments.models import ScheduleFrequency
from pbip_api.modules.payments.recurrence import compute_next_occurrence

pytestmark = pytest.mark.unit


def test_first_emi_is_one_month_after_disbursal() -> None:
    assert due_dates(date(2026, 11, 2), 3) == [
        date(2026, 12, 2),
        date(2027, 1, 2),
        date(2027, 2, 2),
    ]


def test_month_end_disbursal_clamps_and_does_not_drift() -> None:
    # Disbursed 31 Dec: first EMI 31 Jan (anchor day 31), then 28 Feb, back to 31 Mar.
    assert due_dates(date(2026, 12, 31), 4) == [
        date(2027, 1, 31),
        date(2027, 2, 28),
        date(2027, 3, 31),
        date(2027, 4, 30),
    ]
    # Disbursed 31 Jan: the first EMI is clamped (28 Feb), and that day anchors the rest.
    assert due_dates(date(2027, 1, 31), 3) == [
        date(2027, 2, 28),
        date(2027, 3, 28),
        date(2027, 4, 28),
    ]
    assert due_dates(date(2028, 1, 31), 1) == [date(2028, 2, 29)]  # leap year


def test_a_thirty_year_schedule_has_one_date_per_month() -> None:
    dates = due_dates(date(2026, 11, 2), 360)
    assert len(dates) == 360 and dates[-1] == date(2056, 11, 2)
    assert all(later > earlier for earlier, later in pairwise(dates))


def test_rejects_a_non_positive_tenure() -> None:
    with pytest.raises(ValueError, match="tenure must be positive"):
        due_dates(date(2026, 11, 2), 0)


def test_matches_the_scheduled_transfers_engine_monthly_recurrence() -> None:
    """The engine projects the loan instruction from its start date (the first due date)."""
    day = date(2026, 1, 1)
    for _ in range(400):  # every day of a year and more, including all month ends
        dates = due_dates(day, 24)
        engine = [dates[0]]
        for _month in range(23):
            following = compute_next_occurrence(dates[0], engine[-1], ScheduleFrequency.MONTHLY)
            assert following is not None
            engine.append(following)
        assert dates == engine, day
        day += timedelta(days=1)
