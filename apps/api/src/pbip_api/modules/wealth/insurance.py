"""Insurance eligibility hints (TASK-096, ADR-0028) and the simulated quote rule (ADR-0058):
pure functions, no customer data beyond what the customer declares in the request.

A hint says which ADR-0015 age bands overlap a product's entry-age range, and whether every age
in the band can apply or only some of them. It is NOT underwriting: it never looks at a customer,
and an insurer would still decide eligibility and the premium. The band boundaries come from
``customers.demographics.band_for_age`` so they can never drift from the profile's bands.
"""

from dataclasses import dataclass
from fractions import Fraction
from typing import Final

from pbip_api.modules.customers.demographics import MAX_AGE, MIN_AGE, AgeBand, band_for_age
from pbip_api.modules.wealth.models import InsuranceApplicationStatus


@dataclass(frozen=True, slots=True)
class BandHint:
    age_band: AgeBand
    whole_band_eligible: bool  # False: only part of the band is inside the entry ages


def _band_ages() -> dict[AgeBand, range]:
    """Each band's ages within the ages the bank accepts (18 to 120), in band order."""
    bands: dict[AgeBand, list[int]] = {}
    for age in range(MIN_AGE, MAX_AGE + 1):
        bands.setdefault(band_for_age(age), []).append(age)
    return {band: range(ages[0], ages[-1] + 1) for band, ages in bands.items()}


_BAND_AGES = _band_ages()


def eligible_age_bands(min_entry_age: int, max_entry_age: int) -> list[BandHint]:
    """The bands overlapping ``[min_entry_age, max_entry_age]``, youngest first."""
    if min_entry_age > max_entry_age:
        raise ValueError("min_entry_age must not exceed max_entry_age")
    hints: list[BandHint] = []
    for band, ages in _BAND_AGES.items():
        if ages.stop - 1 < min_entry_age or ages.start > max_entry_age:
            continue
        whole = min_entry_age <= ages.start and ages.stop - 1 <= max_entry_age
        hints.append(BandHint(age_band=band, whole_band_eligible=whole))
    return hints


# ------------------------------------------------------- simulated quote (ADR-0058 section 8)
# NOT underwriting and NOT an insurer's price: a stated, versioned rule that scales the product's
# one indicative premium (ADR-0028). Changing a factor is a new QUOTE_VERSION.
QUOTE_VERSION: Final = "IQ-1"
FULL_BPS: Final = 10_000
AGE_FACTOR_BPS: Final[dict[AgeBand, int]] = {
    AgeBand.AGE_18_25: 8_000,
    AgeBand.AGE_26_35: 10_000,
    AgeBand.AGE_36_50: 16_000,
    AgeBand.AGE_51_59: 26_000,
    AgeBand.AGE_60_PLUS: 40_000,
}
TERM_STEP_BPS: Final = 150  # term life: +1.5% of the premium per year above the reference term
MIN_TERM_FACTOR_BPS: Final = 5_000
REFERRAL_COVER_MULTIPLE: Final = 2


@dataclass(frozen=True, slots=True)
class PlanRules:
    """The catalogue facts the quote reads (``insurance_products`` columns)."""

    term_life: bool
    indicative_premium_minor: int
    reference_cover_minor: int
    reference_age: int
    reference_term_years: int
    min_entry_age: int
    max_entry_age: int
    max_cover_age: int | None


@dataclass(frozen=True, slots=True)
class QuoteDecision:
    status: InsuranceApplicationStatus
    reason: str
    annual_premium_minor: int | None  # None when declined
    age_factor_bps: int
    term_factor_bps: int


def band_ages(band: AgeBand) -> range:
    """Every accepted age (18 to 120) in a band."""
    return _BAND_AGES[band]


def term_factor_bps(rules: PlanRules, term_years: int) -> int:
    if not rules.term_life:
        return FULL_BPS
    step = TERM_STEP_BPS * (term_years - rules.reference_term_years)
    return max(MIN_TERM_FACTOR_BPS, FULL_BPS + step)


def premium_minor(rules: PlanRules, cover_minor: int, term_years: int, band: AgeBand) -> int:
    """The indicative premium scaled by cover, age band and (term life) term, half up to paise."""
    reference_band = band_for_age(rules.reference_age)
    value = (
        Fraction(rules.indicative_premium_minor)
        * Fraction(cover_minor, rules.reference_cover_minor)
        * Fraction(AGE_FACTOR_BPS[band], AGE_FACTOR_BPS[reference_band])
        * Fraction(term_factor_bps(rules, term_years), FULL_BPS)
    )
    return max(1, int(value + Fraction(1, 2)))


def _referral(rules: PlanRules, cover_minor: int, term_years: int, band: AgeBand) -> str | None:
    ages = band_ages(band)
    youngest = max(ages.start, rules.min_entry_age)
    oldest = min(ages.stop - 1, rules.max_entry_age)
    if youngest != ages.start or oldest != ages.stop - 1:
        return "Only part of your age band can apply, so an insurer would need your exact age."
    if rules.max_cover_age is not None and oldest + term_years > rules.max_cover_age:
        return (
            "At some ages in your band this term would run past the plan's maximum cover age, "
            "so an insurer would need your exact age."
        )
    if cover_minor > REFERRAL_COVER_MULTIPLE * rules.reference_cover_minor:
        return "Cover above twice the reference cover is referred for medical underwriting."
    return None


def decide(rules: PlanRules, cover_minor: int, term_years: int, band: AgeBand) -> QuoteDecision:
    """The documented, deterministic status rules (ADR-0058 section 8), in order."""
    if band == AgeBand.UNKNOWN:
        raise ValueError("a quote needs a declared age band")
    ages = band_ages(band)
    youngest = max(ages.start, rules.min_entry_age)
    oldest = min(ages.stop - 1, rules.max_entry_age)
    age_factor, term_factor = AGE_FACTOR_BPS[band], term_factor_bps(rules, term_years)
    if youngest > oldest:
        return QuoteDecision(
            InsuranceApplicationStatus.DECLINED,
            f"Your age band is outside this plan's entry ages ({rules.min_entry_age} to "
            f"{rules.max_entry_age}).",
            None,
            age_factor,
            term_factor,
        )
    if rules.max_cover_age is not None and youngest + term_years > rules.max_cover_age:
        return QuoteDecision(
            InsuranceApplicationStatus.DECLINED,
            f"At every eligible age in your band a {term_years}-year term would run past the "
            f"plan's maximum cover age of {rules.max_cover_age}.",
            None,
            age_factor,
            term_factor,
        )
    premium = premium_minor(rules, cover_minor, term_years, band)
    referral = _referral(rules, cover_minor, term_years, band)
    if referral is not None:
        return QuoteDecision(
            InsuranceApplicationStatus.SUBMITTED, referral, premium, age_factor, term_factor
        )
    return QuoteDecision(
        InsuranceApplicationStatus.APPROVED_SIMULATED,
        "Meets the simulated acceptance rules. No policy is issued and nothing is charged.",
        premium,
        age_factor,
        term_factor,
    )
