from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from pbip_ml.forecasting.contract import Forecast, ForecastKind, ForecastPoint
from pbip_ml.fraud.contract import (
    FraudDecision,
    FraudReason,
    FraudScoreResult,
    ReasonSource,
    TransactionContext,
)
from pbip_ml.fraud.testing import AllowAllScorer
from pbip_ml.recommendations.contract import ProductType, Recommendation
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def make_context(**overrides: object) -> TransactionContext:
    base: dict[str, object] = {
        "transaction_id": uuid4(),
        "customer_id": uuid4(),
        "account_id": uuid4(),
        "amount_minor": 250_000,
        "currency": "INR",
        "occurred_at": datetime.now(UTC),
    }
    base.update(overrides)
    return TransactionContext(**base)  # type: ignore[arg-type]


def test_allow_all_scorer_satisfies_the_contract() -> None:
    result = AllowAllScorer().score(make_context())
    assert result.decision is FraudDecision.ALLOW
    assert 0 <= result.risk_score <= 100
    assert result.model_version


@pytest.mark.parametrize("bad", [0, -5])
def test_non_positive_amounts_are_rejected(bad: int) -> None:
    with pytest.raises(ValidationError):
        make_context(amount_minor=bad)


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValidationError):
        make_context(occurred_at=datetime(2026, 1, 1, 12, 0))  # noqa: DTZ001


@pytest.mark.parametrize("score", [-1, 101])
def test_risk_score_bounds_are_enforced(score: int) -> None:
    with pytest.raises(ValidationError):
        FraudScoreResult(
            risk_score=score,
            decision=FraudDecision.ALLOW,
            reasons=[],
            model_version="v",
            latency_ms=1.0,
        )


def test_reasons_are_part_of_the_result() -> None:
    reason = FraudReason(
        code="NEW_PAYEE", message="Payee added 2 minutes ago", source=ReasonSource.RULE
    )
    result = FraudScoreResult(
        risk_score=91,
        decision=FraudDecision.REVIEW,
        reasons=[reason],
        model_version="rules-1+lgbm-3",
        latency_ms=12.5,
    )
    assert result.reasons[0].code == "NEW_PAYEE"


def test_forecast_points_must_be_ordered_and_intervals_are_mandatory() -> None:
    with pytest.raises(ValidationError):
        ForecastPoint(period_start=date(2026, 2, 1), lower=10, median=5, upper=20)
    forecast = Forecast(
        kind=ForecastKind.MONTHLY_SPEND,
        confidence_level=0.8,
        points=[ForecastPoint(period_start=date(2026, 2, 1), lower=10, median=15, upper=20)],
        model_version="v1",
        method="ets",
    )
    assert forecast.points[0].lower <= forecast.points[0].upper


def test_recommendations_require_reasons() -> None:
    with pytest.raises(ValidationError):
        Recommendation(
            product_id=uuid4(),
            product_type=ProductType.LOAN,
            score=0.7,
            reasons=[],
            model_version="v",
        )
