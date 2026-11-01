"""Unit tests for fraud telemetry and decision statistics schemas and caching (TASK-035)."""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pbip_api.modules.fraud.schemas import (
    DailyDecisionPoint,
    DecisionSummary,
    FraudStatsResponse,
    HighRiskPayeeStat,
    TriggeredRuleStat,
)
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError

pytestmark = pytest.mark.unit


def test_fraud_stats_response_serialization() -> None:
    """Validate full FraudStatsResponse model serialization and bounds."""
    payee_id = uuid4()
    resp = FraudStatsResponse(
        period_days=30,
        total_decisions=100,
        decision_counts={"ALLOW": 80, "STEP_UP": 10, "REVIEW": 8, "BLOCK": 2},
        hold_rate=0.08,
        block_rate=0.02,
        summary=DecisionSummary(
            allow=80,
            step_up=10,
            review=8,
            block=2,
            total=100,
            hold_rate=0.08,
            block_rate=0.02,
            step_up_rate=0.10,
        ),
        timeline=[
            DailyDecisionPoint(
                date="2026-10-31",
                allow=40,
                step_up=5,
                review=4,
                block=1,
                total=50,
            ),
            DailyDecisionPoint(
                date="2026-11-01",
                allow=40,
                step_up=5,
                review=4,
                block=1,
                total=50,
            ),
        ],
        rules_breakdown=[
            TriggeredRuleStat(rule_code="VELOCITY_EXCEEDED", count=12, percentage=12.0),
            TriggeredRuleStat(rule_code="HIGH_AMOUNT", count=8, percentage=8.0),
        ],
        high_risk_payees=[
            HighRiskPayeeStat(
                payee_id=payee_id,
                payee_name="Suspicious Global Corp",
                bank_code="BARC0001",
                flagged_count=5,
                avg_risk_score=87.5,
            )
        ],
    )

    assert resp.total_decisions == 100
    assert resp.hold_rate == 0.08
    assert resp.block_rate == 0.02
    assert len(resp.timeline) == 2
    assert len(resp.rules_breakdown) == 2
    assert len(resp.high_risk_payees) == 1
    assert resp.high_risk_payees[0].payee_id == payee_id


def test_fraud_stats_empty_database_schema() -> None:
    """Zero decisions result in clean zero rates without division errors."""
    resp = FraudStatsResponse(
        period_days=30,
        total_decisions=0,
        decision_counts={"ALLOW": 0, "STEP_UP": 0, "REVIEW": 0, "BLOCK": 0},
        hold_rate=0.0,
        block_rate=0.0,
        summary=DecisionSummary(
            allow=0,
            step_up=0,
            review=0,
            block=0,
            total=0,
            hold_rate=0.0,
            block_rate=0.0,
            step_up_rate=0.0,
        ),
        timeline=[],
        rules_breakdown=[],
        high_risk_payees=[],
    )
    assert resp.total_decisions == 0
    assert resp.hold_rate == 0.0
    assert resp.summary.total == 0


def test_fraud_stats_validation_bounds() -> None:
    """Invalid bounds (e.g. negative counts or hold_rate > 1.0) must fail validation."""
    with pytest.raises(ValidationError):
        DecisionSummary(
            allow=-1,
            step_up=0,
            review=0,
            block=0,
            total=0,
            hold_rate=0.0,
            block_rate=0.0,
            step_up_rate=0.0,
        )

    with pytest.raises(ValidationError):
        DecisionSummary(
            allow=10,
            step_up=0,
            review=0,
            block=0,
            total=10,
            hold_rate=1.5,  # must be le 1.0
            block_rate=0.0,
            step_up_rate=0.0,
        )


def test_router_serves_from_redis_cache() -> None:
    """When cache key exists, endpoint returns cached model directly without querying DB."""
    from pbip_api.modules.auth.dependencies import Principal
    from pbip_api.modules.fraud.router import get_fraud_stats

    cached_obj = FraudStatsResponse(
        period_days=30,
        total_decisions=42,
        decision_counts={"ALLOW": 40, "STEP_UP": 2, "REVIEW": 0, "BLOCK": 0},
        hold_rate=0.0,
        block_rate=0.0,
        summary=DecisionSummary(
            allow=40,
            step_up=2,
            review=0,
            block=0,
            total=42,
            hold_rate=0.0,
            block_rate=0.0,
            step_up_rate=0.0476,
        ),
        timeline=[],
        rules_breakdown=[],
        high_risk_payees=[],
    )
    redis_mock = MagicMock()
    redis_mock.get.return_value = cached_obj.model_dump_json()

    principal = Principal(
        user_id=uuid4(),
        roles=frozenset({"FRAUD_ANALYST"}),
        amr=frozenset({"pwd", "otp"}),
        token_id="token-123",
    )
    db_session_mock = MagicMock()

    result = get_fraud_stats(
        days=30,
        _analyst=principal,
        session=db_session_mock,
        redis_client=redis_mock,
    )

    redis_mock.get.assert_called_once_with("fraud:stats:days:30")
    db_session_mock.execute.assert_not_called()
    assert result.total_decisions == 42


def test_router_handles_redis_failure_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    """When Redis is unreachable, endpoint falls through to DB and does not crash."""
    from pbip_api.modules.auth.dependencies import Principal
    from pbip_api.modules.fraud.router import get_fraud_stats

    redis_mock = MagicMock()
    redis_mock.get.side_effect = RedisConnectionError("Redis connection refused")
    redis_mock.setex.side_effect = RedisConnectionError("Redis connection refused")

    principal = Principal(
        user_id=uuid4(),
        roles=frozenset({"FRAUD_ANALYST"}),
        amr=frozenset({"pwd", "otp"}),
        token_id="token-123",
    )
    db_session_mock = MagicMock()

    expected = FraudStatsResponse(
        period_days=30,
        total_decisions=5,
        decision_counts={"ALLOW": 5, "STEP_UP": 0, "REVIEW": 0, "BLOCK": 0},
        hold_rate=0.0,
        block_rate=0.0,
        summary=DecisionSummary(
            allow=5,
            step_up=0,
            review=0,
            block=0,
            total=5,
            hold_rate=0.0,
            block_rate=0.0,
            step_up_rate=0.0,
        ),
        timeline=[],
        rules_breakdown=[],
        high_risk_payees=[],
    )
    monkeypatch.setattr(
        "pbip_api.modules.fraud.service.get_fraud_stats",
        lambda session, days: expected,
    )

    result = get_fraud_stats(
        days=30,
        _analyst=principal,
        session=db_session_mock,
        redis_client=redis_mock,
    )

    assert result.total_decisions == 5
