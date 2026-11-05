"""TASK-096 / ADR-0028: insurance eligibility hints are derived from entry ages only."""

from dataclasses import replace

import pytest
from pbip_api.modules.customers.demographics import AgeBand, band_for_age
from pbip_api.modules.wealth import insurance
from pbip_api.modules.wealth.insurance import BandHint, eligible_age_bands
from pbip_api.modules.wealth.models import InsuranceApplicationStatus as Status

pytestmark = pytest.mark.unit


def _hints(lo: int, hi: int) -> list[tuple[str, bool]]:
    return [(h.age_band.value, h.whole_band_eligible) for h in eligible_age_bands(lo, hi)]


def test_band_for_age_uses_the_profile_boundaries() -> None:
    assert [band_for_age(a) for a in (17, 18, 25, 26, 35, 36, 50, 51, 59, 60, 120)] == [
        AgeBand.UNKNOWN,
        AgeBand.AGE_18_25,
        AgeBand.AGE_18_25,
        AgeBand.AGE_26_35,
        AgeBand.AGE_26_35,
        AgeBand.AGE_36_50,
        AgeBand.AGE_36_50,
        AgeBand.AGE_51_59,
        AgeBand.AGE_51_59,
        AgeBand.AGE_60_PLUS,
        AgeBand.AGE_60_PLUS,
    ]


def test_entry_ages_18_to_60_cover_four_bands_whole_and_60_plus_only_in_part() -> None:
    assert _hints(18, 60) == [
        ("18-25", True),
        ("26-35", True),
        ("36-50", True),
        ("51-59", True),
        ("60+", False),  # 60 can apply, 61 cannot
    ]


def test_a_range_inside_one_band_is_partial_and_band_edges_are_exact() -> None:
    assert _hints(60, 75) == [("60+", False)]
    assert _hints(26, 35) == [("26-35", True)]
    assert _hints(25, 26) == [("18-25", False), ("26-35", False)]
    assert _hints(60, 120) == [("60+", True)]


def test_unknown_is_never_a_hint_and_an_inverted_range_is_refused() -> None:
    assert all(h.age_band is not AgeBand.UNKNOWN for h in eligible_age_bands(18, 120))
    assert eligible_age_bands(30, 30) == [BandHint(AgeBand.AGE_26_35, False)]
    with pytest.raises(ValueError, match="must not exceed"):
        eligible_age_bands(40, 30)


# ------------------------------------------------- simulated quote rule IQ-1 (ADR-0058 s. 8)

LAKH = 1_00_000_00
CRORE = 100 * LAKH
TERM_CLASSIC = insurance.PlanRules(
    term_life=True,
    indicative_premium_minor=12_500_00,
    reference_cover_minor=CRORE,
    reference_age=30,
    reference_term_years=30,
    min_entry_age=18,
    max_entry_age=60,
    max_cover_age=75,
)
HEALTH_INDIVIDUAL = insurance.PlanRules(
    term_life=False,
    indicative_premium_minor=7_800_00,
    reference_cover_minor=5 * LAKH,
    reference_age=30,
    reference_term_years=1,
    min_entry_age=18,
    max_entry_age=65,
    max_cover_age=None,
)
HEALTH_SENIOR = insurance.PlanRules(
    term_life=False,
    indicative_premium_minor=32_000_00,
    reference_cover_minor=5 * LAKH,
    reference_age=65,
    reference_term_years=1,
    min_entry_age=60,
    max_entry_age=75,
    max_cover_age=None,
)


@pytest.mark.parametrize(
    ("rules", "cover", "term", "band", "status", "premium"),
    [
        # the reference profile itself: exactly the indicative premium
        (TERM_CLASSIC, CRORE, 30, AgeBand.AGE_26_35, Status.APPROVED_SIMULATED, 12_500_00),
        # 0.80 for 18-25 and 1 + 1.5% x 10 years = 1.15 for the term: 12,500 x 0.92
        (TERM_CLASSIC, CRORE, 40, AgeBand.AGE_18_25, Status.APPROVED_SIMULATED, 11_500_00),
        # 1.60 for 36-50; at 50 a 30-year term passes 75: referred, premium still quoted
        (TERM_CLASSIC, CRORE, 30, AgeBand.AGE_36_50, Status.SUBMITTED, 20_000_00),
        # at 51 a 30-year term already passes 75: declined, no premium
        (TERM_CLASSIC, CRORE, 30, AgeBand.AGE_51_59, Status.DECLINED, None),
        # only age 60 of the 60+ band can enter: referred
        (TERM_CLASSIC, CRORE, 10, AgeBand.AGE_60_PLUS, Status.SUBMITTED, 35_000_00),  # x4.00 x0.70
        # cover above twice the reference: referred for medicals
        (TERM_CLASSIC, 3 * CRORE, 30, AgeBand.AGE_26_35, Status.SUBMITTED, 37_500_00),
        # health: exactly twice the reference cover is not above it; term factor is 1
        (HEALTH_INDIVIDUAL, 10 * LAKH, 1, AgeBand.AGE_36_50, Status.APPROVED_SIMULATED, 24_960_00),
        (HEALTH_SENIOR, 5 * LAKH, 1, AgeBand.AGE_51_59, Status.DECLINED, None),
        (HEALTH_SENIOR, 5 * LAKH, 1, AgeBand.AGE_60_PLUS, Status.SUBMITTED, 32_000_00),
    ],
)
def test_quote_rule_iq1_premiums_and_statuses(
    rules: insurance.PlanRules,
    cover: int,
    term: int,
    band: AgeBand,
    status: Status,
    premium: int | None,
) -> None:
    decision = insurance.decide(rules, cover, term, band)
    assert (decision.status, decision.annual_premium_minor) == (status, premium)
    assert decision.reason


def test_the_term_factor_has_a_floor_and_health_plans_ignore_the_term() -> None:
    assert insurance.term_factor_bps(TERM_CLASSIC, 30) == 10_000
    assert insurance.term_factor_bps(TERM_CLASSIC, 10) == 7_000
    long_reference = replace(TERM_CLASSIC, reference_term_years=70)
    assert insurance.term_factor_bps(long_reference, 10) == 5_000  # 1 - 90% floored at 0.50
    assert insurance.term_factor_bps(HEALTH_INDIVIDUAL, 1) == 10_000


def test_a_quote_needs_a_declared_band() -> None:
    with pytest.raises(ValueError, match="declared age band"):
        insurance.decide(TERM_CLASSIC, CRORE, 30, AgeBand.UNKNOWN)
