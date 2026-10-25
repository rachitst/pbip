from datetime import date, timedelta

import pytest
from pbip_datagen.calendars import (
    ELECTRICITY_SEASONALITY,
    IST,
    add_months,
    at_ist,
    clamp_day,
    last_business_day,
    month_starts,
    next_business_day,
)

pytestmark = pytest.mark.unit


def test_add_months_rolls_over_year_boundaries() -> None:
    assert add_months(date(2025, 11, 17), 3) == date(2026, 2, 1)
    assert month_starts(date(2025, 12, 1), 3) == [
        date(2025, 12, 1),
        date(2026, 1, 1),
        date(2026, 2, 1),
    ]


def test_clamp_day_handles_short_months_and_leap_years() -> None:
    assert clamp_day(date(2025, 2, 1), 31) == date(2025, 2, 28)
    assert clamp_day(date(2024, 2, 1), 31) == date(2024, 2, 29)
    assert clamp_day(date(2025, 4, 1), 0) == date(2025, 4, 1)


def test_last_business_day_skips_weekends() -> None:
    assert last_business_day(date(2025, 5, 1)) == date(2025, 5, 30)  # 31st is a Saturday
    assert last_business_day(date(2025, 8, 1)) == date(2025, 8, 29)  # 30-31 are Sat-Sun
    assert last_business_day(date(2025, 7, 1)) == date(2025, 7, 31)  # Thursday


def test_next_business_day_moves_weekend_debits_forward() -> None:
    assert next_business_day(date(2025, 6, 1)) == date(2025, 6, 2)  # Sunday -> Monday
    assert next_business_day(date(2025, 6, 4)) == date(2025, 6, 4)


def test_at_ist_is_timezone_aware_and_clamped() -> None:
    moment = at_ist(date(2025, 1, 1), 9 * 60 + 30)
    assert moment.tzinfo is IST and moment.utcoffset() == timedelta(hours=5, minutes=30)
    assert (moment.hour, moment.minute) == (9, 30)
    assert at_ist(date(2025, 1, 1), 99_999).hour == 23


def test_electricity_peaks_in_the_indian_summer() -> None:
    assert max(ELECTRICITY_SEASONALITY, key=ELECTRICITY_SEASONALITY.__getitem__) == 5
    assert set(ELECTRICITY_SEASONALITY) == set(range(1, 13))
