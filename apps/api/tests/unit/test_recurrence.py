"""Unit tests for scheduled transfer recurrence and occurrence projection (ADR-0021)."""

from datetime import date

import pytest
from pbip_api.modules.payments.models import ScheduleFrequency
from pbip_api.modules.payments.recurrence import (
    compute_missed_occurrences,
    compute_next_occurrence,
)


def test_daily_recurrence() -> None:
    start = date(2026, 1, 1)
    nxt1 = compute_next_occurrence(start, start, ScheduleFrequency.DAILY)
    assert nxt1 == date(2026, 1, 2)

    nxt2 = compute_next_occurrence(start, nxt1, ScheduleFrequency.DAILY)
    assert nxt2 == date(2026, 1, 3)


def test_weekly_recurrence() -> None:
    start = date(2026, 1, 5)  # Monday
    nxt1 = compute_next_occurrence(start, start, ScheduleFrequency.WEEKLY)
    assert nxt1 == date(2026, 1, 12)  # Following Monday

    nxt2 = compute_next_occurrence(start, nxt1, ScheduleFrequency.WEEKLY)
    assert nxt2 == date(2026, 1, 19)


def test_monthly_recurrence_standard() -> None:
    start = date(2026, 1, 15)
    nxt1 = compute_next_occurrence(start, start, ScheduleFrequency.MONTHLY)
    assert nxt1 == date(2026, 2, 15)

    nxt2 = compute_next_occurrence(start, nxt1, ScheduleFrequency.MONTHLY)
    assert nxt2 == date(2026, 3, 15)


def test_monthly_recurrence_jan_31_non_leap_year() -> None:
    """Non-leap year: Jan 31 -> Feb 28 -> Mar 31 -> Apr 30 -> May 31."""
    start = date(2026, 1, 31)

    feb = compute_next_occurrence(start, start, ScheduleFrequency.MONTHLY)
    assert feb == date(2026, 2, 28)

    mar = compute_next_occurrence(start, feb, ScheduleFrequency.MONTHLY)
    assert mar == date(2026, 3, 31)  # Anchored to start.day=31, no drift to 28

    apr = compute_next_occurrence(start, mar, ScheduleFrequency.MONTHLY)
    assert apr == date(2026, 4, 30)

    may = compute_next_occurrence(start, apr, ScheduleFrequency.MONTHLY)
    assert may == date(2026, 5, 31)


def test_monthly_recurrence_jan_31_leap_year() -> None:
    """Leap year 2024: Jan 31 -> Feb 29 -> Mar 31."""
    start = date(2024, 1, 31)

    feb = compute_next_occurrence(start, start, ScheduleFrequency.MONTHLY)
    assert feb == date(2024, 2, 29)

    mar = compute_next_occurrence(start, feb, ScheduleFrequency.MONTHLY)
    assert mar == date(2024, 3, 31)


def test_monthly_recurrence_jan_30() -> None:
    """Jan 30 -> Feb 28 -> Mar 30."""
    start = date(2026, 1, 30)

    feb = compute_next_occurrence(start, start, ScheduleFrequency.MONTHLY)
    assert feb == date(2026, 2, 28)

    mar = compute_next_occurrence(start, feb, ScheduleFrequency.MONTHLY)
    assert mar == date(2026, 3, 30)


def test_monthly_recurrence_december_to_january() -> None:
    """Advance across calendar year boundary."""
    start = date(2026, 12, 15)
    jan = compute_next_occurrence(start, start, ScheduleFrequency.MONTHLY)
    assert jan == date(2027, 1, 15)


def test_recurrence_respects_end_date() -> None:
    start = date(2026, 1, 1)
    end = date(2026, 1, 10)

    # Within bounds
    nxt = compute_next_occurrence(start, date(2026, 1, 9), ScheduleFrequency.DAILY, end_date=end)
    assert nxt == date(2026, 1, 10)

    # Exceeds bounds
    expired = compute_next_occurrence(
        start, date(2026, 1, 10), ScheduleFrequency.DAILY, end_date=end
    )
    assert expired is None


def test_invalid_current_date_before_start() -> None:
    with pytest.raises(ValueError, match="cannot be before start_date"):
        compute_next_occurrence(date(2026, 5, 1), date(2026, 4, 1), ScheduleFrequency.DAILY)


def test_unsupported_frequency() -> None:
    with pytest.raises(ValueError, match="unsupported recurrence frequency"):
        compute_next_occurrence(date(2026, 1, 1), date(2026, 1, 1), "BIWEEKLY")  # type: ignore[arg-type]


def test_compute_missed_occurrences_none() -> None:
    start = date(2026, 1, 1)
    # next_run_date == today_utc -> nothing missed
    assert (
        compute_missed_occurrences(
            start, date(2026, 1, 5), ScheduleFrequency.DAILY, today_utc=date(2026, 1, 5)
        )
        == []
    )
    # next_run_date > today_utc -> nothing missed
    assert (
        compute_missed_occurrences(
            start, date(2026, 1, 6), ScheduleFrequency.DAILY, today_utc=date(2026, 1, 5)
        )
        == []
    )


def test_compute_missed_occurrences_multiple() -> None:
    start = date(2026, 1, 1)
    # 3 days overdue: Jan 1, Jan 2, Jan 3 (today is Jan 4)
    missed = compute_missed_occurrences(
        start, date(2026, 1, 1), ScheduleFrequency.DAILY, today_utc=date(2026, 1, 4)
    )
    assert missed == [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]


def test_compute_missed_occurrences_bounded_by_end_date() -> None:
    start = date(2026, 1, 1)
    end = date(2026, 1, 2)
    # Today is Jan 10, but end_date was Jan 2
    missed = compute_missed_occurrences(
        start, date(2026, 1, 1), ScheduleFrequency.DAILY, today_utc=date(2026, 1, 10), end_date=end
    )
    assert missed == [date(2026, 1, 1), date(2026, 1, 2)]
