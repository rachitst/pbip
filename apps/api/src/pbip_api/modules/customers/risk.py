"""Risk-tolerance questionnaire and its scoring (TASK-097, FR-33, UC-07 input).

The questionnaire is deliberately short and every answer is an enumerated choice, so the band is
reproducible from the stored answers alone: each answer is worth 0, 1 or 2 points, the five points
are summed (0-10) and the total maps to a band by fixed cut-offs. No other data (balances, age,
segment) is mixed in, so a customer can see exactly why they got their band and change it by
answering again. Changing a question, an answer's points or a cut-off is a new
``QUESTIONNAIRE_VERSION``; stored answers keep the version that scored them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

QUESTIONNAIRE_VERSION: Final = "RTQ-1"


class RiskBand(StrEnum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


class InvestmentHorizon(StrEnum):
    """How long the customer expects to leave money invested."""

    UNDER_1_YEAR = "UNDER_1_YEAR"
    ONE_TO_5_YEARS = "ONE_TO_5_YEARS"
    OVER_5_YEARS = "OVER_5_YEARS"


class LossReaction(StrEnum):
    """What the customer would do if their investments fell 20% in a year."""

    SELL_EVERYTHING = "SELL_EVERYTHING"
    WAIT_AND_HOLD = "WAIT_AND_HOLD"
    INVEST_MORE = "INVEST_MORE"


class InvestmentGoal(StrEnum):
    PROTECT_SAVINGS = "PROTECT_SAVINGS"
    STEADY_GROWTH = "STEADY_GROWTH"
    MAXIMUM_GROWTH = "MAXIMUM_GROWTH"


class InvestmentExperience(StrEnum):
    NONE = "NONE"
    SOME = "SOME"
    EXTENSIVE = "EXTENSIVE"


class EmergencyFund(StrEnum):
    """Months of essential spending the customer could cover without investments."""

    UNDER_3_MONTHS = "UNDER_3_MONTHS"
    THREE_TO_6_MONTHS = "THREE_TO_6_MONTHS"
    OVER_6_MONTHS = "OVER_6_MONTHS"


# Every answer enum lists its members from most cautious (0 points) to least cautious (2 points);
# the points are the member's position, so the order above IS the scoring table.
_POINTS_PER_ANSWER_MAX: Final = 2
MAX_SCORE: Final = 5 * _POINTS_PER_ANSWER_MAX
LOW_MAX_SCORE: Final = 3  # 0-3 -> LOW
MODERATE_MAX_SCORE: Final = 6  # 4-6 -> MODERATE, 7-10 -> HIGH


@dataclass(frozen=True, slots=True)
class RiskAnswers:
    investment_horizon: InvestmentHorizon
    loss_reaction: LossReaction
    investment_goal: InvestmentGoal
    investment_experience: InvestmentExperience
    emergency_fund: EmergencyFund

    def as_dict(self) -> dict[str, str]:
        return {
            "investment_horizon": self.investment_horizon.value,
            "loss_reaction": self.loss_reaction.value,
            "investment_goal": self.investment_goal.value,
            "investment_experience": self.investment_experience.value,
            "emergency_fund": self.emergency_fund.value,
        }


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    band: RiskBand
    score: int


def _points(answer: StrEnum) -> int:
    return list(type(answer)).index(answer)


def band_for_score(score: int) -> RiskBand:
    if not 0 <= score <= MAX_SCORE:
        raise ValueError("risk score out of range")
    if score <= LOW_MAX_SCORE:
        return RiskBand.LOW
    if score <= MODERATE_MAX_SCORE:
        return RiskBand.MODERATE
    return RiskBand.HIGH


def score_answers(answers: RiskAnswers) -> RiskAssessment:
    """Sum the points of the five answers and map the total to a band."""
    score = sum(
        _points(answer)
        for answer in (
            answers.investment_horizon,
            answers.loss_reaction,
            answers.investment_goal,
            answers.investment_experience,
            answers.emergency_fund,
        )
    )
    return RiskAssessment(band=band_for_score(score), score=score)
