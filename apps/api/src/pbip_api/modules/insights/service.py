"""Relationship Manager insights (TASK-056, TASK-066): churn signals, eligible products, outreach.

Cost is bounded: one set-based query gives every customer's balances and last activity, the churn
signals are computed from those rows, and only the requested page gets product recommendations.
The one exception is the loan-propensity filter: a customer's loan score exists only after the
recommendation rules have run, so with that filter every candidate is scored (ADR-0022).
The ordering is by churn risk (retention priority), then balance.

Scope (ADR-0032): an administrator sees every customer; a Relationship Manager sees only the
customers assigned to them, and any other customer is a 404 like an unknown one (``policy.py``).
Only an administrator assigns, through ``assign_rm``/``unassign_rm``, which keep the history and
write the audit chain in the same transaction.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from pbip_ml.recommendations import PRODUCT_CATALOGUE
from sqlalchemy.orm import Session

from pbip_api.core.db import utcnow
from pbip_api.core.errors import NotFoundError, UnprocessableError
from pbip_api.modules.accounts import service as accounts_service
from pbip_api.modules.analytics import service as analytics_service
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.insights import policy, repository
from pbip_api.modules.insights.policy import RmScope
from pbip_api.modules.insights.schemas import (
    AssignedRm,
    ChurnRiskLevel,
    CustomerInsightDetail,
    CustomerInsightItem,
    CustomerInsightsListResponse,
    HealthSummary,
    InsightScope,
    InteractionCreate,
    InteractionListResponse,
    InteractionOut,
    RecommendedProduct,
    RelationshipManagerListResponse,
    RelationshipManagerOut,
    RmAssignmentOut,
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
    scope: RmScope,
    min_churn_risk: float | None = None,
    segment: str | None = None,
    limit: int = 20,
    offset: int = 0,
    now: datetime | None = None,
    min_loan_propensity: float | None = None,
) -> CustomerInsightsListResponse:
    """One page of the customers in ``scope``, ordered by churn risk, with signals and eligible
    products. The scope is applied before filtering and paging, so ``total`` is within it."""
    eval_time = now or utcnow()
    visible = policy.visible_customer_ids(session, scope)
    listed = InsightScope.ALL if visible is None else InsightScope.ASSIGNED
    scored = [
        row
        for row in (
            _score(activity, eval_time)
            for activity in analytics_service.get_customer_activity(
                session, eval_time, BALANCE_WINDOW
            )
            if visible is None or activity.customer_id in visible
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
        return CustomerInsightsListResponse(
            items=items, total=len(scored), evaluated_at=eval_time, scope=listed
        )

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
        items=matching[offset : offset + limit],
        total=len(matching),
        evaluated_at=eval_time,
        scope=listed,
    )


def get_customer_insight_detail(
    session: Session, scope: RmScope, customer_id: UUID, now: datetime | None = None
) -> CustomerInsightDetail:
    """The flyout for one customer; 404 when the customer does not exist or is out of scope."""
    policy.require_visible_customer(session, scope, customer_id)
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
        assigned_rm=assigned_rm(session, customer_id),
    )


MAX_INTERACTIONS_PAGE = 50


def list_interactions(
    session: Session, scope: RmScope, customer_id: UUID, limit: int = 20
) -> InteractionListResponse:
    """The newest outreach records for a customer; 404 when unknown or out of scope. The log stays
    with the customer, so a newly assigned RM reads what the previous one recorded."""
    policy.require_visible_customer(session, scope, customer_id)
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
    scope: RmScope,
    customer_id: UUID,
    body: InteractionCreate,
    *,
    ip: str | None = None,
    request_id: str | None = None,
) -> InteractionOut:
    """Record an outreach and audit it in the same transaction (the note itself is not audited).
    404 when the customer is unknown or out of the caller's scope."""
    policy.require_visible_customer(session, scope, customer_id)
    staff_user_id = scope.user_id
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


def assigned_rm(session: Session, customer_id: UUID) -> AssignedRm | None:
    """The customer's current RM, or None (also used by the demo seed to stay idempotent)."""
    found = repository.active_assignment(session, customer_id)
    if found is None:
        return None
    row, email = found
    return AssignedRm(rm_user_id=row.rm_user_id, rm_email=email, assigned_at=row.assigned_at)


def list_relationship_managers(session: Session) -> RelationshipManagerListResponse:
    """Active RMs (id and email) for the administrator's assignment picker."""
    return RelationshipManagerListResponse(
        items=[
            RelationshipManagerOut(user_id=user_id, email=email)
            for user_id, email in repository.active_relationship_managers(session)
        ]
    )


def assign_rm(
    session: Session,
    customer_id: UUID,
    rm_user_id: UUID,
    admin_user_id: UUID,
    *,
    now: datetime | None = None,
    ip: str | None = None,
    request_id: str | None = None,
) -> RmAssignmentOut:
    """Make ``rm_user_id`` the customer's one active RM (ADR-0032); idempotent for the same RM.

    The per-customer assignment lock is taken before the active row is read, so concurrent
    administrators are serialised and exactly one assignment stays active. A reassignment closes
    the previous row (history is kept) and both facts go into one ``RM_ASSIGNED`` audit row.
    """
    at = now or utcnow()
    if repository.get_customer(session, customer_id) is None:
        raise NotFoundError("customer not found")
    rm_email = repository.active_relationship_manager(session, rm_user_id)
    if rm_email is None:
        raise UnprocessableError("rm_user_id is not an active relationship manager")
    repository.lock_customer_assignment(session, customer_id)
    current = repository.active_assignment(session, customer_id)
    if current is not None and current[0].rm_user_id == rm_user_id:
        unchanged = RmAssignmentOut(
            customer_id=customer_id,
            assigned_rm=AssignedRm(
                rm_user_id=rm_user_id, rm_email=rm_email, assigned_at=current[0].assigned_at
            ),
        )
        session.rollback()  # nothing changed: release the lock, write nothing
        return unchanged
    previous = None
    if current is not None:
        previous = current[0].rm_user_id
        repository.close_assignment(session, current[0], admin_user_id, at)
    row = repository.add_assignment(session, customer_id, rm_user_id, admin_user_id, at)
    AuditService(session).record(
        actor_user_id=admin_user_id,
        action="RM_ASSIGNED",
        resource_type="customer",
        resource_id=str(customer_id),
        ip=ip,
        request_id=request_id,
        details={
            "rm_user_id": str(rm_user_id),
            "previous_rm_user_id": None if previous is None else str(previous),
        },
    )
    out = RmAssignmentOut(
        customer_id=customer_id,
        assigned_rm=AssignedRm(
            rm_user_id=rm_user_id, rm_email=rm_email, assigned_at=row.assigned_at
        ),
    )
    session.commit()
    return out


def unassign_rm(
    session: Session,
    customer_id: UUID,
    admin_user_id: UUID,
    *,
    now: datetime | None = None,
    ip: str | None = None,
    request_id: str | None = None,
) -> None:
    """End the customer's active assignment (kept as history) and audit ``RM_UNASSIGNED``.

    Idempotent: with no active assignment nothing is written, so a retry is safe. 404 for an
    unknown customer.
    """
    at = now or utcnow()
    if repository.get_customer(session, customer_id) is None:
        raise NotFoundError("customer not found")
    repository.lock_customer_assignment(session, customer_id)
    current = repository.active_assignment(session, customer_id)
    if current is None:
        session.rollback()
        return
    row = current[0]
    repository.close_assignment(session, row, admin_user_id, at)
    AuditService(session).record(
        actor_user_id=admin_user_id,
        action="RM_UNASSIGNED",
        resource_type="customer",
        resource_id=str(customer_id),
        ip=ip,
        request_id=request_id,
        details={"rm_user_id": str(row.rm_user_id)},
    )
    session.commit()
