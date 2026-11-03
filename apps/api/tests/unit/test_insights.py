"""Unit tests for Relationship Manager behavioral insights (TASK-056)."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pbip_api.modules.insights.policy import scope_for
from pbip_api.modules.insights.schemas import (
    ChurnRiskLevel,
    CustomerInsightItem,
    CustomerInsightsListResponse,
    InsightOrder,
    InsightScope,
)
from pbip_api.modules.insights.service import (
    calculate_churn_signals,
    compute_balance_decline_rate,
    compute_inactivity_days,
    propensity_key,
)
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
        segment="HIGH_NET_WORTH",
        total_balance_minor=500_000_00,
        inactivity_days=12,
        balance_decline_rate=0.15,
        churn_risk_score=0.25,
        churn_risk_level=ChurnRiskLevel.LOW,
        top_recommended_product="Balanced Index Wealth SIP",
        top_product_score=0.88,
        high_value=True,
    )
    assert item.full_name == "Alice Trader"
    assert item.churn_risk_level == ChurnRiskLevel.LOW
    assert item.high_value is True


def test_customer_insight_item_schema_invalid_bounds() -> None:
    """Invalid bounds in scores are rejected."""
    with pytest.raises(ValidationError):
        CustomerInsightItem(
            customer_id=uuid4(),
            full_name="Bob",
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
        scope=InsightScope.ASSIGNED,
        order=InsightOrder.PROPENSITY,
        product_score_version="rules-v1",
    )
    assert resp.total == 0
    assert resp.scope == InsightScope.ASSIGNED
    assert (resp.order, resp.product_score_version) == (InsightOrder.PROPENSITY, "rules-v1")
    assert resp.items == []


def test_balance_decline_rate_exact_values() -> None:
    assert compute_balance_decline_rate(100_000_00, 60_000_00) == 0.4
    assert compute_balance_decline_rate(100_000_00, 100_000_00) == 0.0
    assert compute_balance_decline_rate(100_000_00, 150_000_00) == 0.0  # grew: no decline
    assert compute_balance_decline_rate(0, 50_000_00) == 0.0  # nothing to decline from
    assert compute_balance_decline_rate(30_000_00, 0) == 1.0
    assert compute_balance_decline_rate(30_000_00, 10_000_00) == 0.6667


def test_inactivity_days_exact_values() -> None:
    joined = datetime(2026, 9, 1, tzinfo=UTC)
    now = datetime(2026, 10, 31, 12, tzinfo=UTC)
    assert compute_inactivity_days(datetime(2026, 10, 21, 12, tzinfo=UTC), joined, now) == 10
    assert compute_inactivity_days(None, joined, now) == 60  # since joining
    assert compute_inactivity_days(now, joined, now) == 0


def test_churn_score_exact_value() -> None:
    # 0.55 * 0.4 + 0.45 * (10 / 60) = 0.22 + 0.075
    assert calculate_churn_signals(inactivity_days=10, balance_decline_rate=0.4) == (
        0.295,
        ChurnRiskLevel.LOW,
    )


def test_scope_is_all_for_admin_and_assigned_for_an_rm() -> None:
    """ADR-0032: ADMIN wins for a user holding both roles; any other staff role is portfolio-only."""
    user = uuid4()
    assert scope_for(user, {"ADMIN"}).sees_all
    assert scope_for(user, {"ADMIN", "RELATIONSHIP_MANAGER"}).sees_all
    rm = scope_for(user, frozenset({"RELATIONSHIP_MANAGER"}))
    assert (rm.user_id, rm.sees_all) == (user, False)
    assert not scope_for(user, []).sees_all


def _row(
    product_score: float | None, churn: float = 0.0, balance: int = 0, customer_id: str = ""
) -> CustomerInsightItem:
    return CustomerInsightItem(
        customer_id=UUID(customer_id) if customer_id else uuid4(),
        full_name="Test Customer",
        segment="UNSEGMENTED",
        total_balance_minor=balance,
        inactivity_days=0,
        balance_decline_rate=0.0,
        churn_risk_score=churn,
        churn_risk_level=ChurnRiskLevel.LOW,
        top_recommended_product=None if product_score is None else "Fixed deposit",
        top_product_score=product_score,
    )


def test_propensity_order_is_score_then_churn_then_balance_then_id() -> None:
    """TASK-056: best product score first, no eligible product last, and a fixed tie-break so the
    same data always pages the same way."""
    low_id, high_id = "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"
    none_but_risky = _row(None, churn=0.9, balance=10**9)
    best = _row(0.91)
    tie_riskier = _row(0.6, churn=0.5)
    tie_richer = _row(0.6, churn=0.2, balance=500)
    tie_poorer_low_id = _row(0.6, churn=0.2, balance=100, customer_id=low_id)
    tie_poorer_high_id = _row(0.6, churn=0.2, balance=100, customer_id=high_id)
    rows = [
        tie_poorer_high_id,
        none_but_risky,
        tie_richer,
        tie_poorer_low_id,
        best,
        tie_riskier,
    ]
    expected = [
        best,
        tie_riskier,
        tie_richer,
        tie_poorer_low_id,
        tie_poorer_high_id,
        none_but_risky,
    ]
    assert sorted(rows, key=propensity_key) == expected
