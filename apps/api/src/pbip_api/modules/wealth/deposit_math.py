"""Fixed-deposit arithmetic in integer paise (TASK-064, ADR-0025 section 2).

A cumulative deposit compounds QUARTERLY (the Indian bank convention) and every tenure is a whole
number of quarters, so the maturity amount is P (1 + r/4)^q. Interest accrued part-way through a
quarter adds simple interest for the days since the last completed quarter (actual/365), so the
accrued figure on the maturity date is exactly the projected interest. Everything is evaluated with
exact ``Fraction`` arithmetic and rounded half up ONCE, at the end, to whole paise: rounding each
quarter would drift by a paisa a quarter on long deposits.

Quarters are calendar quarters from the start date, clamped to the month's last day (a deposit
opened on 31 Aug reaches its first quarter on 30 Nov), so dates never drift.
"""

import calendar
from dataclasses import dataclass
from datetime import date
from fractions import Fraction

QUARTERS_PER_YEAR = 4
MONTHS_PER_QUARTER = 3
DAYS_PER_YEAR = 365
NO_INTEREST_DAYS = 7  # closed within a week of booking: no interest (RBI practice)


@dataclass(frozen=True, slots=True)
class Maturity:
    maturity_date: date
    interest_minor: int
    maturity_amount_minor: int


def round_half_up(value: Fraction) -> int:
    if value < 0:
        return -round_half_up(-value)
    return int((value + Fraction(1, 2)).__floor__())


def add_months(start: date, months: int) -> date:
    """``start`` plus whole calendar months, clamped to the month's last day (no drift: always
    measured from ``start``, never chained)."""
    if months < 0:
        raise ValueError("months must be non-negative")
    index = start.month - 1 + months
    year, month = start.year + index // 12, index % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def _quarterly_rate(annual_rate_bps: int) -> Fraction:
    return Fraction(annual_rate_bps, 10_000 * QUARTERS_PER_YEAR)


def _check(principal_minor: int, annual_rate_bps: int) -> None:
    if principal_minor <= 0:
        raise ValueError("principal must be positive")
    if annual_rate_bps < 0:
        raise ValueError("rate must be non-negative")


def apy_bps(annual_rate_bps: int) -> int:
    """Annual percentage yield of a quarterly-compounded rate, in whole basis points."""
    if annual_rate_bps < 0:
        raise ValueError("rate must be non-negative")
    growth = (1 + _quarterly_rate(annual_rate_bps)) ** QUARTERS_PER_YEAR
    return round_half_up((growth - 1) * 10_000)


def maturity(
    principal_minor: int, annual_rate_bps: int, tenure_months: int, start: date
) -> Maturity:
    _check(principal_minor, annual_rate_bps)
    if tenure_months <= 0 or tenure_months % MONTHS_PER_QUARTER:
        raise ValueError("tenure must be a positive whole number of quarters")
    quarters = tenure_months // MONTHS_PER_QUARTER
    amount = round_half_up(principal_minor * (1 + _quarterly_rate(annual_rate_bps)) ** quarters)
    return Maturity(add_months(start, tenure_months), amount - principal_minor, amount)


def accrued_interest(
    principal_minor: int, annual_rate_bps: int, start: date, maturity_date: date, on: date
) -> int:
    """Interest earned from ``start`` to ``on`` (capped at ``maturity_date``): compounded for each
    completed quarter, simple interest for the days of the running quarter."""
    _check(principal_minor, annual_rate_bps)
    on = min(on, maturity_date)
    if on <= start:
        return 0
    quarters = 0
    while add_months(start, (quarters + 1) * MONTHS_PER_QUARTER) <= on:
        quarters += 1
    days = (on - add_months(start, quarters * MONTHS_PER_QUARTER)).days
    rate = Fraction(annual_rate_bps, 10_000)
    value = (
        principal_minor
        * (1 + _quarterly_rate(annual_rate_bps)) ** quarters
        * (1 + rate * days / DAYS_PER_YEAR)
    )
    return round_half_up(value) - principal_minor


def premature_interest(
    principal_minor: int,
    annual_rate_bps: int,
    penalty_bps: int,
    start: date,
    maturity_date: date,
    on: date,
) -> int:
    """Interest paid when a deposit is closed before maturity: accrued at the contract rate minus
    the penalty (never below 0%) for the time actually held; nothing within the first week."""
    if penalty_bps < 0:
        raise ValueError("penalty must be non-negative")
    if (on - start).days < NO_INTEREST_DAYS:
        return 0
    reduced = max(annual_rate_bps - penalty_bps, 0)
    return accrued_interest(principal_minor, reduced, start, maturity_date, on)
