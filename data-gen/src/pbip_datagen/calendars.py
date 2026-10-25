"""Banking calendar rules: when salaries land, when EMIs and bills are debited, seasonal effects.

All dates are Indian calendar dates; times of day are generated in IST and stored as UTC.
Weekends are the only non-business days modelled (no public-holiday calendar - documented
simplification).
"""

import calendar as _calendar
from datetime import date, datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

# Electricity load peaks with air-conditioning in the Indian summer (Apr-Jun) and dips in the
# monsoon; mild winter heating in Dec-Jan. Multipliers apply to the customer's base bill.
ELECTRICITY_SEASONALITY: dict[int, float] = {
    1: 1.10, 2: 1.00, 3: 1.10, 4: 1.35, 5: 1.60, 6: 1.45,
    7: 1.10, 8: 0.95, 9: 0.95, 10: 1.00, 11: 1.00, 12: 1.10,
}  # fmt: skip

# Festive season (Navratri/Diwali in Oct-Nov, year-end in Dec) lifts discretionary spending.
FESTIVE_SPEND_MULTIPLIER: dict[int, float] = {10: 1.30, 11: 1.35, 12: 1.15}

# Annual appraisal cycle: salaries are revised in April (financial year start).
APPRAISAL_MONTH = 4


def add_months(day: date, months: int) -> date:
    """First day of the month ``months`` after ``day``'s month."""
    index = day.year * 12 + (day.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def month_starts(start: date, months: int) -> list[date]:
    first = start.replace(day=1)
    return [add_months(first, offset) for offset in range(months)]


def days_in_month(month_start: date) -> int:
    return _calendar.monthrange(month_start.year, month_start.month)[1]


def clamp_day(month_start: date, day: int) -> date:
    """``day`` of the month, clamped to the month's length (an EMI on the 31st in February)."""
    return month_start.replace(day=min(max(day, 1), days_in_month(month_start)))


def is_business_day(day: date) -> bool:
    return day.weekday() < 5


def last_business_day(month_start: date) -> date:
    """Salaries in India are typically credited on the last working day of the month."""
    day = clamp_day(month_start, 31)
    while not is_business_day(day):
        day -= timedelta(days=1)
    return day


def next_business_day(day: date) -> date:
    """Pensions and bill debits falling on a weekend move to the following working day."""
    while not is_business_day(day):
        day += timedelta(days=1)
    return day


def at_ist(day: date, minute_of_day: int) -> datetime:
    """A timezone-aware instant: ``minute_of_day`` minutes after IST midnight on ``day``."""
    minute_of_day = min(max(minute_of_day, 0), 24 * 60 - 1)
    return datetime.combine(day, time(minute_of_day // 60, minute_of_day % 60), tzinfo=IST)
