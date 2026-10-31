"""Relationship Manager insights (TASK-056): churn signals and the top eligible product.

Cost is bounded: one set-based query gives every customer's balances and last activity, the churn
signals are computed from those rows, and only the requested page gets a product recommendation.
The ordering is by churn risk (retention priority), then balance.
"""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from pbip_api.core.db import utcnow
from pbip_api.modules.analytics import service as analytics_service
from pbip_api.modules.insights.schemas import (
    ChurnRiskLevel,
    CustomerInsightItem,
    CustomerInsightsListResponse,
)
from pbip_api.modules.recommendations import service as recommendations_service

BALANCE_WINDOW = timedelta(days=30)
INACTIVITY_HORIZON_DAYS = 60
HIGH_VALUE_BALANCE_MINOR = 100_000_00  # INR 1,00,000
NO_SEGMENT = "UNSEGMENTED"


def compute_balance_decline_rate(balance_window_start: int, balance_now: int) -> float:
    """Share of the balance lost over the window: (start - now) / start, 0 if it did not fall."""
    if balance_window_start <= 0 or balance_now >= balance_window_start:
        return 0.0
    return round((balance_window_start - max(balance_now, 0)) / balance_window_start, 4)


def compute_inactivity_days(
    last_activity: datetime | None, created_at: datetime, now: datetime
) -> int:
    """Whole days since the last login, ledger entry or event (since joining if none)."""
    return max((now - (last_activity or created_at)).days, 0)


def calculate_churn_signals(
    inactivity_days: int, balance_decline_rate: float
) -> tuple[float, ChurnRiskLevel]:
    """Rule-based churn score: 55% balance decline + 45% inactivity (capped at 60 days)."""
    inactivity_norm = min(max(inactivity_days / INACTIVITY_HORIZON_DAYS, 0.0), 1.0)
    decline_norm = min(max(balance_decline_rate, 0.0), 1.0)
    score = round(0.55 * decline_norm + 0.45 * inactivity_norm, 3)
    if score >= 0.70:
        return score, ChurnRiskLevel.HIGH
    if score >= 0.40:
        return score, ChurnRiskLevel.MEDIUM
    return score, ChurnRiskLevel.LOW


def _top_product(
    session: Session, customer_id: UUID, now: datetime
) -> tuple[str | None, float | None]:
    recommendations = recommendations_service.get_customer_recommendations(
        session, customer_id, now
    )
    if not recommendations.recommendations:
        return None, None
    top = recommendations.recommendations[0]
    return top.title, top.score


def get_customer_insights(
    session: Session,
    min_churn_risk: float | None = None,
    segment: str | None = None,
    limit: int = 20,
    offset: int = 0,
    now: datetime | None = None,
) -> CustomerInsightsListResponse:
    """One page of customers ordered by churn risk, with signals and the top eligible product."""
    eval_time = now or utcnow()
    scored = []
    for activity in analytics_service.get_customer_activity(session, eval_time, BALANCE_WINDOW):
        segment_name = activity.segment or NO_SEGMENT
        if segment and segment_name.upper() != segment.upper():
            continue
        decline = compute_balance_decline_rate(
            activity.balance_window_start_minor, activity.balance_minor
        )
        inactive = compute_inactivity_days(
            activity.last_activity_at, activity.created_at, eval_time
        )
        score, level = calculate_churn_signals(inactive, decline)
        if min_churn_risk is not None and score < min_churn_risk:
            continue
        scored.append((activity, segment_name, decline, inactive, score, level))

    scored.sort(key=lambda row: (-row[4], -row[0].balance_minor, str(row[0].customer_id)))
    items = []
    for activity, segment_name, decline, inactive, score, level in scored[offset : offset + limit]:
        title, product_score = _top_product(session, activity.customer_id, eval_time)
        items.append(
            CustomerInsightItem(
                customer_id=activity.customer_id,
                full_name=activity.full_name,
                segment=segment_name,
                total_balance_minor=activity.balance_minor,
                inactivity_days=inactive,
                balance_decline_rate=decline,
                churn_risk_score=score,
                churn_risk_level=level,
                top_recommended_product=title,
                top_product_score=product_score,
                high_value=activity.balance_minor >= HIGH_VALUE_BALANCE_MINOR
                or segment_name == "HIGH_NET_WORTH",
            )
        )
    return CustomerInsightsListResponse(items=items, total=len(scored), evaluated_at=eval_time)
