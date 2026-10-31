"""Service layer for Relationship Manager behavioral insights (TASK-056)."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from pbip_api.core.db import utcnow
from pbip_api.core.errors import DomainError
from pbip_api.modules.accounts import service as accounts_service
from pbip_api.modules.analytics import service as analytics_service
from pbip_api.modules.auth.models import User
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.events import repository as events_repository
from pbip_api.modules.insights.schemas import (
    ChurnRiskLevel,
    CustomerInsightItem,
    CustomerInsightsListResponse,
)
from pbip_api.modules.recommendations import service as recommendations_service


def calculate_churn_signals(
    inactivity_days: int,
    balance_decline_rate: float,
) -> tuple[float, ChurnRiskLevel]:
    """Calculate combined churn risk score and categorical rating.

    Weighted formula:
    - 55% weight on balance depletion rate (capital flight indicator)
    - 45% weight on inactivity period (disengagement indicator, normalized to 60 days)
    """
    inactivity_norm = min(max(inactivity_days / 60.0, 0.0), 1.0)
    decline_norm = min(max(balance_decline_rate, 0.0), 1.0)

    score = round(0.55 * decline_norm + 0.45 * inactivity_norm, 3)

    if score >= 0.70:
        level = ChurnRiskLevel.HIGH
    elif score >= 0.40:
        level = ChurnRiskLevel.MEDIUM
    else:
        level = ChurnRiskLevel.LOW

    return score, level


def _compute_inactivity_days(
    last_login: datetime | None,
    latest_event_time: datetime | None,
    created_at: datetime,
    eval_time: datetime,
) -> int:
    """Compute days elapsed since the customer's last recorded session or telemetry."""
    activity_timestamps = [ts for ts in (last_login, latest_event_time) if ts is not None]
    if activity_timestamps:
        most_recent = max(activity_timestamps)
        return max((eval_time - most_recent).days, 0)
    return max((eval_time - created_at).days, 0)


def _compute_balance_decline_rate(
    session: Session,
    customer_id: UUID,
    primary_acc_id: UUID | None,
    total_balance: int,
) -> float:
    """Estimate capital decline rate based on historical spending breakdown."""
    if primary_acc_id is None:
        return 0.0

    try:
        breakdown = analytics_service.get_spending_breakdown(
            session=session,
            customer_id=customer_id,
            account_id=primary_acc_id,
        )
        past_spend = breakdown.total_spend_minor
    except (DomainError, ValueError):
        past_spend = 0

    baseline = total_balance + past_spend
    if baseline > 0 and total_balance < baseline:
        return float(round((baseline - total_balance) / baseline, 4))
    if total_balance == 0 and past_spend > 0:
        return 1.0
    return 0.0


def _compute_top_recommendation(
    session: Session,
    customer_id: UUID,
    eval_time: datetime,
) -> tuple[str | None, float | None]:
    """Retrieve top propensity banking product from recommendation engine."""
    try:
        recs_resp = recommendations_service.get_customer_recommendations(
            session=session,
            customer_id=customer_id,
            now=eval_time,
        )
        if recs_resp.recommendations:
            top_rec = recs_resp.recommendations[0]
            return top_rec.title, top_rec.score
    except (DomainError, ValueError):
        pass
    return None, None


def _resolve_customer_segment(
    session: Session,
    customer_id: UUID,
    fallback_segment: str | None,
) -> str:
    """Determine customer behavioral segment with fallback."""
    try:
        seg_record = analytics_service.get_customer_segment(session, customer_id)
        return seg_record.segment
    except (DomainError, ValueError):
        return fallback_segment or "BUDGET_CONSCIOUS"


def get_customer_insights(
    session: Session,
    min_churn_risk: float | None = None,
    segment: str | None = None,
    limit: int = 50,
    offset: int = 0,
    now: datetime | None = None,
) -> CustomerInsightsListResponse:
    """Aggregate behavioral profile, churn signals, and propensity ranking for RM staff."""
    eval_time = now or utcnow()

    stmt = (
        select(Customer, User.email, User.last_login_at)
        .join(User, Customer.user_id == User.id)
        .order_by(Customer.created_at.desc())
    )
    rows = session.execute(stmt).all()

    all_items: list[CustomerInsightItem] = []

    for customer, email, last_login in rows:
        customer_id: UUID = customer.id
        accounts = accounts_service.list_accounts(session, customer_id)
        total_balance = sum(acc.available_balance for acc in accounts)
        primary_acc_id = accounts[0].id if accounts else None

        segment_name = _resolve_customer_segment(session, customer_id, customer.segment)
        if segment and segment_name.upper() != segment.upper():
            continue

        recent_events = events_repository.list_events_by_customer(session, customer_id, limit=1)
        latest_event_time = recent_events[0].event_timestamp if recent_events else None

        inactivity_days = _compute_inactivity_days(
            last_login=last_login,
            latest_event_time=latest_event_time,
            created_at=customer.created_at,
            eval_time=eval_time,
        )

        balance_decline = _compute_balance_decline_rate(
            session=session,
            customer_id=customer_id,
            primary_acc_id=primary_acc_id,
            total_balance=total_balance,
        )

        churn_score, churn_level = calculate_churn_signals(inactivity_days, balance_decline)
        if min_churn_risk is not None and churn_score < min_churn_risk:
            continue

        top_title, top_score = _compute_top_recommendation(session, customer_id, eval_time)
        high_value = total_balance >= 100_000_00 or segment_name == "HIGH_NET_WORTH"

        all_items.append(
            CustomerInsightItem(
                customer_id=customer_id,
                full_name=customer.full_name,
                email=email,
                segment=segment_name,
                total_balance_minor=total_balance,
                inactivity_days=inactivity_days,
                balance_decline_rate=balance_decline,
                churn_risk_score=churn_score,
                churn_risk_level=churn_level,
                top_recommended_product=top_title,
                top_propensity_score=top_score,
                high_value_transactor=high_value,
            )
        )

    # Prioritize customers: high propensity first, then high churn risk
    all_items.sort(
        key=lambda item: (
            item.top_propensity_score if item.top_propensity_score is not None else 0.0,
            item.churn_risk_score,
        ),
        reverse=True,
    )

    total_count = len(all_items)
    paginated_items = all_items[offset : offset + limit]

    return CustomerInsightsListResponse(
        items=paginated_items,
        total=total_count,
        evaluated_at=eval_time,
    )
