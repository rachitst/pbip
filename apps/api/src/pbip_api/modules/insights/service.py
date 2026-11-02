"""Relationship Manager insights (TASK-056, TASK-066): churn signals, eligible products, outreach.

Cost is bounded: one set-based query gives every customer's balances and last activity, the churn
signals are computed from those rows, and only the requested page gets product recommendations.
The one exception is the loan-propensity filter: a customer's loan score exists only after the
recommendation rules have run, so with that filter every candidate is scored (ADR-0022).
The ordering is by churn risk (retention priority), then balance.

Scope: RM and admin see every customer. The bank has no RM-to-customer assignment yet, and the
requirements (UC-12, actors table) do not define one; ADR-0022 records this.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from pbip_ml.recommendations import PRODUCT_CATALOGUE
from sqlalchemy.orm import Session

from pbip_api.core.db import utcnow
from pbip_api.core.errors import NotFoundError
from pbip_api.modules.accounts import service as accounts_service
from pbip_api.modules.analytics import service as analytics_service
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.insights import repository
from pbip_api.modules.insights.schemas import (
    ChurnRiskLevel,
    CustomerInsightDetail,
    CustomerInsightItem,
    CustomerInsightsListResponse,
    HealthSummary,
    InteractionCreate,
    InteractionListResponse,
    InteractionOut,
    RecommendedProduct,
    TransactionVolumeOut,
)
from pbip_api.modules.recommendations import service as recommendations_service
from pbip_api.modules.recommendations.schemas import ProductRecommendationItem

BALANCE_WINDOW = timedelta(days=30)
INACTIVITY_HORIZON_DAYS = 60
HIGH_VALUE_BALANCE_MINOR = 100_000_00  # INR 1,00,000
NO_SEGMENT = "UNSEGMENTED"
VOLUME_WINDOW = timedelta(days=30)
LOAN = "LOAN"


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


@dataclass(frozen=True)
class _Scored:
    activity: analytics_service.CustomerActivity
    segment: str
    decline: float
    inactive_days: int
    score: float
    level: ChurnRiskLevel


def _score(activity: analytics_service.CustomerActivity, now: datetime) -> _Scored:
    decline = compute_balance_decline_rate(
        activity.balance_window_start_minor, activity.balance_minor
    )
    inactive = compute_inactivity_days(activity.last_activity_at, activity.created_at, now)
    score, level = calculate_churn_signals(inactive, decline)
    return _Scored(activity, activity.segment or NO_SEGMENT, decline, inactive, score, level)


def _ranked_products(
    session: Session, customer_id: UUID, now: datetime
) -> list[ProductRecommendationItem]:
    """Every eligible product, best first (the customer route only shows the top 3)."""
    return recommendations_service.get_customer_recommendations(
        session, customer_id, now, top_k=len(PRODUCT_CATALOGUE)
    ).recommendations


def _item(row: _Scored, products: list[ProductRecommendationItem]) -> CustomerInsightItem:
    top = products[0] if products else None
    loan = next((p for p in products if p.product_type == LOAN), None)
    return CustomerInsightItem(
        customer_id=row.activity.customer_id,
        full_name=row.activity.full_name,
        segment=row.segment,
        total_balance_minor=row.activity.balance_minor,
        inactivity_days=row.inactive_days,
        balance_decline_rate=row.decline,
        churn_risk_score=row.score,
        churn_risk_level=row.level,
        top_recommended_product=top.title if top else None,
        top_product_score=top.score if top else None,
        loan_product=loan.title if loan else None,
        loan_propensity_score=loan.score if loan else None,
        high_value=row.activity.balance_minor >= HIGH_VALUE_BALANCE_MINOR
        or row.segment == "HIGH_NET_WORTH",
    )


def _matches(row: _Scored, segment: str | None, min_churn_risk: float | None) -> bool:
    if segment and row.segment.upper() != segment.upper():
        return False
    return min_churn_risk is None or row.score >= min_churn_risk


def get_customer_insights(
    session: Session,
    min_churn_risk: float | None = None,
    segment: str | None = None,
    limit: int = 20,
    offset: int = 0,
    now: datetime | None = None,
    min_loan_propensity: float | None = None,
) -> CustomerInsightsListResponse:
    """One page of customers ordered by churn risk, with signals and their eligible products."""
    eval_time = now or utcnow()
    scored = [
        row
        for row in (
            _score(activity, eval_time)
            for activity in analytics_service.get_customer_activity(
                session, eval_time, BALANCE_WINDOW
            )
        )
        if _matches(row, segment, min_churn_risk)
    ]
    scored.sort(key=lambda r: (-r.score, -r.activity.balance_minor, str(r.activity.customer_id)))

    if min_loan_propensity is None:
        page = scored[offset : offset + limit]
        items = [
            _item(row, _ranked_products(session, row.activity.customer_id, eval_time))
            for row in page
        ]
        return CustomerInsightsListResponse(items=items, total=len(scored), evaluated_at=eval_time)

    candidates = [
        _item(row, _ranked_products(session, row.activity.customer_id, eval_time)) for row in scored
    ]
    matching = [
        item
        for item in candidates
        if item.loan_propensity_score is not None
        and item.loan_propensity_score >= min_loan_propensity
    ]
    return CustomerInsightsListResponse(
        items=matching[offset : offset + limit], total=len(matching), evaluated_at=eval_time
    )


def get_customer_insight_detail(
    session: Session, customer_id: UUID, now: datetime | None = None
) -> CustomerInsightDetail:
    """The flyout for one customer; 404 when the customer does not exist."""
    eval_time = now or utcnow()
    activity = next(
        iter(
            analytics_service.get_customer_activity(session, eval_time, BALANCE_WINDOW, customer_id)
        ),
        None,
    )
    if activity is None:
        raise NotFoundError("customer not found")
    products = _ranked_products(session, customer_id, eval_time)
    accounts = accounts_service.list_accounts(session, customer_id)
    health = None
    if accounts:
        score = analytics_service.get_financial_health_score(
            session, customer_id, accounts[0].id, eval_time
        )
        health = HealthSummary(
            overall_score=score.overall_score,
            rating=score.rating,
            insufficient_data=score.insufficient_data,
        )
    volume = analytics_service.get_transaction_volume(
        session, customer_id, eval_time, VOLUME_WINDOW
    )
    return CustomerInsightDetail(
        insight=_item(_score(activity, eval_time), products),
        customer_since=activity.created_at,
        health=health,
        transaction_volume=TransactionVolumeOut(
            window_days=VOLUME_WINDOW.days,
            transaction_count=volume.transaction_count,
            inflow_minor=volume.inflow_minor,
            outflow_minor=volume.outflow_minor,
        ),
        recommendations=[
            RecommendedProduct(
                title=p.title, product_type=p.product_type, score=p.score, reasons=p.reasons
            )
            for p in products
        ],
        evaluated_at=eval_time,
    )


MAX_INTERACTIONS_PAGE = 50


def list_interactions(
    session: Session, customer_id: UUID, limit: int = 20
) -> InteractionListResponse:
    """The newest outreach records for a customer; 404 when the customer does not exist."""
    if repository.get_customer(session, customer_id) is None:
        raise NotFoundError("customer not found")
    rows = repository.list_interactions(session, customer_id, min(limit, MAX_INTERACTIONS_PAGE))
    return InteractionListResponse(
        items=[
            InteractionOut(
                id=row.id,
                channel=row.channel,
                outcome=row.outcome,
                note=row.note,
                logged_by=email,
                created_at=row.created_at,
            )
            for row, email in rows
        ],
        total=repository.count_interactions(session, customer_id),
    )


def log_interaction(
    session: Session,
    customer_id: UUID,
    staff_user_id: UUID,
    body: InteractionCreate,
    *,
    ip: str | None = None,
    request_id: str | None = None,
) -> InteractionOut:
    """Record an outreach and audit it in the same transaction (the note itself is not audited)."""
    if repository.get_customer(session, customer_id) is None:
        raise NotFoundError("customer not found")
    row = repository.add_interaction(
        session, customer_id, staff_user_id, body.channel, body.outcome, body.note
    )
    AuditService(session).record(
        actor_user_id=staff_user_id,
        action="RM_INTERACTION_LOGGED",
        resource_type="customer",
        resource_id=str(customer_id),
        ip=ip,
        request_id=request_id,
        details={
            "interaction_id": str(row.id),
            "channel": row.channel.value,
            "outcome": row.outcome.value,
        },
    )
    out = InteractionOut(
        id=row.id,
        channel=row.channel,
        outcome=row.outcome,
        note=row.note,
        logged_by=repository.staff_email(session, staff_user_id),
        created_at=row.created_at,
    )
    session.commit()
    return out
