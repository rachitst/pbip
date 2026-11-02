"""The bank's calendar: customer-facing dates are days in India (IST, UTC+05:30).

India has no daylight saving, so a fixed offset is exact and needs no tz database in the image.
"""

from datetime import UTC, date, datetime, time, timedelta, timezone

BANK_TZ = timezone(timedelta(hours=5, minutes=30), "IST")


def day_start(day: date) -> datetime:
    """The instant (UTC) a bank day begins."""
    return datetime.combine(day, time.min, BANK_TZ).astimezone(UTC)


def bank_today(now: datetime) -> date:
    return now.astimezone(BANK_TZ).date()
