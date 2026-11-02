"""Date of birth rules and the derived age band (TASK-107, ADR-0015).

ADR-0015 lets the bank hold one demographic fact, a self-declared date of birth, and lets the
recommendation engine see only a band derived from it at use time (a stored band would go stale).
These are pure functions so the boundaries can be tested exactly; the service decides WHEN the
band may be used (only with consent).
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Final

# Recorded in the audit trail with every date-of-birth and consent change, so the evidence says
# which version of the policy the customer agreed to. Bump it when ADR-0015's purpose changes.
DEMOGRAPHICS_POLICY_VERSION: Final = "ADR-0015-v1"
CONSENT_PURPOSE: Final = "product_recommendations"

MIN_AGE: Final = 18
MAX_AGE: Final = 120


class AgeBand(StrEnum):
    AGE_18_25 = "18-25"
    AGE_26_35 = "26-35"
    AGE_36_50 = "36-50"
    AGE_51_59 = "51-59"
    AGE_60_PLUS = "60+"
    UNKNOWN = "unknown"


def age_on(date_of_birth: date, today: date) -> int:
    """Completed years on ``today``. Someone born on 29 February turns a year older on 1 March
    in a non-leap year (their birthday has not yet happened on 28 February)."""
    had_birthday = (today.month, today.day) >= (date_of_birth.month, date_of_birth.day)
    return today.year - date_of_birth.year - (0 if had_birthday else 1)


def is_allowed_date_of_birth(date_of_birth: date, today: date) -> bool:
    """Adults only (18) and nothing implausible (over 120); never in the future."""
    return date_of_birth <= today and MIN_AGE <= age_on(date_of_birth, today) <= MAX_AGE


def age_band(date_of_birth: date | None, today: date) -> AgeBand:
    """The ADR-0015 band for a date of birth, or UNKNOWN when there is none.

    An age under 18 cannot be stored (validation), so it is reported as UNKNOWN rather than
    guessed into the youngest band; ages above 120 stay 60+ because age only grows after the
    date was accepted.
    """
    if date_of_birth is None:
        return AgeBand.UNKNOWN
    return band_for_age(age_on(date_of_birth, today))


def band_for_age(age: int) -> AgeBand:
    """The band of a completed age; under 18 is UNKNOWN. Public so other modules (the insurance
    eligibility hints, ADR-0028) use these exact boundaries instead of copying them."""
    if age < MIN_AGE:
        return AgeBand.UNKNOWN
    if age <= 25:
        return AgeBand.AGE_18_25
    if age <= 35:
        return AgeBand.AGE_26_35
    if age <= 50:
        return AgeBand.AGE_36_50
    if age <= 59:
        return AgeBand.AGE_51_59
    return AgeBand.AGE_60_PLUS
