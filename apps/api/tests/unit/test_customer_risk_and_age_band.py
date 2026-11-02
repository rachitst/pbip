"""TASK-097 risk scoring and TASK-107 age band: pure functions, tested at their boundaries."""

from datetime import date
from itertools import product

import pytest
from pbip_api.modules.customers.demographics import (
    AgeBand,
    age_band,
    age_on,
    is_allowed_date_of_birth,
)
from pbip_api.modules.customers.risk import (
    MAX_SCORE,
    EmergencyFund,
    InvestmentExperience,
    InvestmentGoal,
    InvestmentHorizon,
    LossReaction,
    RiskAnswers,
    RiskBand,
    band_for_score,
    score_answers,
)

pytestmark = pytest.mark.unit

TODAY = date(2026, 11, 2)


def _born_years_ago(years: int, today: date = TODAY) -> date:
    return today.replace(year=today.year - years)


# ----------------------------------------------------------------------------- risk scoring
@pytest.mark.parametrize(
    ("score", "band"),
    [
        (0, RiskBand.LOW),
        (3, RiskBand.LOW),
        (4, RiskBand.MODERATE),
        (6, RiskBand.MODERATE),
        (7, RiskBand.HIGH),
        (10, RiskBand.HIGH),
    ],
)
def test_band_cut_offs(score: int, band: RiskBand) -> None:
    assert band_for_score(score) is band


@pytest.mark.parametrize("score", [-1, MAX_SCORE + 1])
def test_scores_outside_the_scale_are_rejected(score: int) -> None:
    with pytest.raises(ValueError, match="out of range"):
        band_for_score(score)


def test_most_cautious_answers_score_zero_and_boldest_score_max() -> None:
    cautious = RiskAnswers(
        InvestmentHorizon.UNDER_1_YEAR,
        LossReaction.SELL_EVERYTHING,
        InvestmentGoal.PROTECT_SAVINGS,
        InvestmentExperience.NONE,
        EmergencyFund.UNDER_3_MONTHS,
    )
    bold = RiskAnswers(
        InvestmentHorizon.OVER_5_YEARS,
        LossReaction.INVEST_MORE,
        InvestmentGoal.MAXIMUM_GROWTH,
        InvestmentExperience.EXTENSIVE,
        EmergencyFund.OVER_6_MONTHS,
    )
    assert score_answers(cautious).score == 0 and score_answers(cautious).band is RiskBand.LOW
    assert score_answers(bold).score == MAX_SCORE and score_answers(bold).band is RiskBand.HIGH


def test_each_answer_is_worth_its_position_and_middle_answers_are_moderate() -> None:
    middle = RiskAnswers(
        InvestmentHorizon.ONE_TO_5_YEARS,
        LossReaction.WAIT_AND_HOLD,
        InvestmentGoal.STEADY_GROWTH,
        InvestmentExperience.SOME,
        EmergencyFund.THREE_TO_6_MONTHS,
    )
    assert score_answers(middle).score == 5
    assert score_answers(middle).band is RiskBand.MODERATE


def test_scoring_is_deterministic_and_total_over_every_combination() -> None:
    """All 243 answer combinations score in range, map to a band, and agree with the cut-offs."""
    seen: set[RiskBand] = set()
    for combo in product(
        InvestmentHorizon, LossReaction, InvestmentGoal, InvestmentExperience, EmergencyFund
    ):
        answers = RiskAnswers(*combo)
        first, second = score_answers(answers), score_answers(answers)
        assert first == second
        expected = sum(list(type(a)).index(a) for a in combo)
        assert first.score == expected and first.band is band_for_score(expected)
        seen.add(first.band)
    assert seen == set(RiskBand)


# ----------------------------------------------------------------------------- age and band
def test_age_counts_completed_years_only() -> None:
    assert age_on(date(2000, 11, 2), TODAY) == 26  # birthday today
    assert age_on(date(2000, 11, 3), TODAY) == 25  # birthday tomorrow


def test_a_29_february_birthday_turns_on_1_march_in_a_common_year() -> None:
    dob = date(2008, 2, 29)
    assert age_on(dob, date(2026, 2, 28)) == 17
    assert age_on(dob, date(2026, 3, 1)) == 18


@pytest.mark.parametrize(
    ("years", "band"),
    [
        (18, AgeBand.AGE_18_25),
        (25, AgeBand.AGE_18_25),
        (26, AgeBand.AGE_26_35),
        (35, AgeBand.AGE_26_35),
        (36, AgeBand.AGE_36_50),
        (50, AgeBand.AGE_36_50),
        (51, AgeBand.AGE_51_59),
        (59, AgeBand.AGE_51_59),
        (60, AgeBand.AGE_60_PLUS),
        (120, AgeBand.AGE_60_PLUS),
        (130, AgeBand.AGE_60_PLUS),  # age only grows after a valid date was accepted
    ],
)
def test_age_band_boundaries(years: int, band: AgeBand) -> None:
    assert age_band(_born_years_ago(years), TODAY) is band


def test_age_band_changes_on_the_birthday_not_before() -> None:
    dob = date(1966, 11, 3)  # 60 tomorrow
    assert age_band(dob, TODAY) is AgeBand.AGE_51_59
    assert age_band(dob, date(2026, 11, 3)) is AgeBand.AGE_60_PLUS


def test_no_date_or_an_under_age_date_is_unknown_never_a_guess() -> None:
    assert age_band(None, TODAY) is AgeBand.UNKNOWN
    assert age_band(_born_years_ago(17), TODAY) is AgeBand.UNKNOWN


def test_band_labels_are_the_adr_0015_values() -> None:
    assert [band.value for band in AgeBand] == [
        "18-25",
        "26-35",
        "36-50",
        "51-59",
        "60+",
        "unknown",
    ]


@pytest.mark.parametrize(
    ("dob", "allowed"),
    [
        (_born_years_ago(18), True),  # 18 today
        (date(2008, 11, 3), False),  # 18 tomorrow
        (_born_years_ago(120), True),
        (date(1905, 11, 1), False),  # 121 since yesterday
        (date(2027, 1, 1), False),  # future
        (TODAY, False),
    ],
)
def test_date_of_birth_must_give_an_age_of_18_to_120(dob: date, allowed: bool) -> None:
    assert is_allowed_date_of_birth(dob, TODAY) is allowed
