"""Unit tests for Relationship Manager behavioral insights (TASK-056)."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pbip_api.modules.insights.schemas import (
    ChurnRiskLevel,
    CustomerInsightItem,
    CustomerInsightsListResponse,
)
from pbip_api.modules.insights.service import calculate_churn_signals
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def test_calculate_churn_signals_low_risk() -> None:
    """Active customer with zero decline has low churn risk."""
    score, level = calculate_churn_signals(inactivity_days=5, balance_decline_rate=0.0)
    assert score < 0.40
    assert level == ChurnRiskLevel.LOW


def test_calculate_churn_signals_medium_risk() -> None:
    """Moderate inactivity and partial balance decline creates medium churn risk."""
    score, level = calculate_churn_signals(inactivity_days=30, balance_decline_rate=0.35)
    assert 0.40 <= score < 0.70
    assert level == ChurnRiskLevel.MEDIUM


def test_calculate_churn_signals_high_risk() -> None:
    """Prolonged inactivity and high capital decline creates high churn risk."""
    score, level = calculate_churn_signals(inactivity_days=75, balance_decline_rate=0.85)
    assert score >= 0.70
    assert level == ChurnRiskLevel.HIGH


def test_calculate_churn_signals_bounds() -> None:
    """Scores are strictly bounded between 0.0 and 1.0 even with extreme input."""
    score_zero, _ = calculate_churn_signals(inactivity_days=0, balance_decline_rate=0.0)
    assert score_zero == 0.0

    score_extreme, _ = calculate_churn_signals(inactivity_days=365, balance_decline_rate=1.5)
    assert score_extreme == 1.0


def test_customer_insight_item_schema_valid() -> None:
    """Valid insight item serializes correctly."""
    item = CustomerInsightItem(
        customer_id=uuid4(),
        full_name="Alice Trader",
        email="alice@example.com",
        segment="HIGH_NET_WORTH",
        total_balance_minor=500_000_00,
        inactivity_days=12,
        balance_decline_rate=0.15,
        churn_risk_score=0.25,
        churn_risk_level=ChurnRiskLevel.LOW,
        top_recommended_product="Balanced Index Wealth SIP",
        top_propensity_score=0.88,
        high_value_transactor=True,
    )
    assert item.full_name == "Alice Trader"
    assert item.churn_risk_level == ChurnRiskLevel.LOW
    assert item.high_value_transactor is True


def test_customer_insight_item_schema_invalid_bounds() -> None:
    """Invalid bounds in scores are rejected."""
    with pytest.raises(ValidationError):
        CustomerInsightItem(
            customer_id=uuid4(),
            full_name="Bob",
            email="bob@example.com",
            segment="BUDGET_CONSCIOUS",
            total_balance_minor=1000,
            inactivity_days=-5,  # must be ge 0
            balance_decline_rate=0.0,
            churn_risk_score=1.5,  # must be le 1.0
            churn_risk_level=ChurnRiskLevel.LOW,
        )


def test_customer_insights_list_response() -> None:
    """Response container formats properly."""
    resp = CustomerInsightsListResponse(
        items=[],
        total=0,
        evaluated_at=datetime.now(UTC),
    )
    assert resp.total == 0
    assert resp.items == []
