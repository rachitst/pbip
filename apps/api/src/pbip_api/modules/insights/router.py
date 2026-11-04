"""FastAPI router for Relationship Manager behavioral insights (TASK-056, TASK-066) and the
RM-to-customer assignment (ADR-0032)."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from pbip_api.core.db import get_session
from pbip_api.modules.analytics import service as analytics_service
from pbip_api.modules.analytics.schemas import (
    BehaviourAnalyticsResponse,
    BehaviourProfileResponse,
    BehaviourSegment,
)
from pbip_api.modules.auth.dependencies import (
    Principal,
    get_clock,
    get_request_meta,
    require_roles,
)
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.insights import policy, service
from pbip_api.modules.insights.policy import RmScope
from pbip_api.modules.insights.schemas import (
    CustomerInsightDetail,
    CustomerInsightsListResponse,
    InsightOrder,
    InteractionCreate,
    InteractionListResponse,
    InteractionOut,
    RelationshipManagerListResponse,
    RmAssignmentOut,
    RmAssignmentRequest,
)

router = APIRouter(prefix="/api/v1/insights", tags=["insights"])

# Public so the route-policy registry test can prove every insights route uses it.
rm_gate = require_roles(
    RoleName.RELATIONSHIP_MANAGER,
    RoleName.ADMIN,
    mfa=True,
)

# Public so the route-policy registry test can prove assignment routes are ADMIN-only (ADR-0032).
assignment_gate = require_roles(RoleName.ADMIN, mfa=True)


def _scope(principal: Principal) -> RmScope:
    """ADMIN sees every customer, an RM only their assigned ones (decided by ``policy``)."""
    return policy.scope_for(principal.user_id, principal.roles)


def _now(clock: Callable[[], float]) -> datetime:
    return datetime.fromtimestamp(clock(), tz=UTC)


@router.get(
    "/customers",
    response_model=CustomerInsightsListResponse,
    status_code=status.HTTP_200_OK,
    summary="Customers in the caller's scope (an RM: assigned only) by churn risk or propensity",
)
def get_prioritized_customer_insights(
    min_churn_risk: float | None = Query(
        None, ge=0.0, le=1.0, description="Minimum churn risk score filter (0.0 to 1.0)"
    ),
    segment: str | None = Query(None, max_length=50, description="Behavioral segment filter"),
    min_loan_propensity: float | None = Query(
        None,
        ge=0.0,
        le=1.0,
        description="Minimum loan propensity (rule-based relevance of the best loan product)",
    ),
    order_by: InsightOrder = Query(
        InsightOrder.CHURN_RISK,
        description=(
            "CHURN_RISK (default): highest churn risk first. PROPENSITY: highest rule-based "
            "product score first (rules-v1, not a trained model); scores every customer in scope"
        ),
    ),
    # Each row on the page runs the recommendation rules, so pages are small.
    limit: int = Query(20, ge=1, le=25, description="Page size"),
    offset: int = Query(0, ge=0, le=100_000, description="Page offset"),
    principal: Principal = Depends(rm_gate),
    session: Session = Depends(get_session),
) -> CustomerInsightsListResponse:
    """One page of customers in the requested order (RM: their assigned customers; admin: all)."""
    return service.get_customer_insights(
        session=session,
        scope=_scope(principal),
        min_churn_risk=min_churn_risk,
        segment=segment,
        limit=limit,
        offset=offset,
        min_loan_propensity=min_loan_propensity,
        order=order_by,
    )


@router.get(
    "/behaviour",
    response_model=BehaviourAnalyticsResponse,
    status_code=status.HTTP_200_OK,
    summary="Behavioural analytics: segment mix, spending, product affinity, engagement",
)
def get_behaviour_analytics(
    days: int = Query(30, ge=7, le=180, description="Period: the last N days (clickstream: 180)"),
    segment: BehaviourSegment | None = Query(None, description="Only this segment"),
    _principal: Principal = Depends(rm_gate),
    session: Session = Depends(get_session),
    clock: Callable[[], float] = Depends(get_clock),
) -> BehaviourAnalyticsResponse:
    """TASK-101 aggregates (RM or admin, MFA). No customer ids: drill-down is ``/customers``."""
    now = datetime.fromtimestamp(clock(), tz=UTC)
    return analytics_service.get_behaviour_analytics(session, now, days, segment)


@router.get(
    "/customers/{customer_id}",
    response_model=CustomerInsightDetail,
    summary="One customer's churn signals, health score, 30-day volume and eligible products",
    responses={404: {"description": "No such customer, or not assigned to the calling RM"}},
)
def get_customer_insight_detail(
    customer_id: UUID,
    principal: Principal = Depends(rm_gate),
    session: Session = Depends(get_session),
) -> CustomerInsightDetail:
    """Detail behind the RM flyout (RM: assigned customers only; admin: any)."""
    return service.get_customer_insight_detail(session, _scope(principal), customer_id)


@router.get(
    "/customers/{customer_id}/behaviour-profile",
    response_model=BehaviourProfileResponse,
    summary="One customer's behavioural profile (UC-11); empty while analytics consent is off",
    responses={404: {"description": "No such customer, or not assigned to the calling RM"}},
)
def get_customer_behaviour_profile(
    customer_id: UUID,
    principal: Principal = Depends(rm_gate),
    session: Session = Depends(get_session),
    clock: Callable[[], float] = Depends(get_clock),
) -> BehaviourProfileResponse:
    """RM: assigned customers only; admin: any (ADR-0032). Consent decides content (ADR-0057)."""
    return service.get_customer_behaviour_profile(
        session, _scope(principal), customer_id, _now(clock)
    )


@router.get(
    "/customers/{customer_id}/interactions",
    response_model=InteractionListResponse,
    summary="Calls, meetings, emails and notes logged for a customer, newest first",
    responses={404: {"description": "No such customer, or not assigned to the calling RM"}},
)
def list_customer_interactions(
    customer_id: UUID,
    limit: int = Query(20, ge=1, le=service.MAX_INTERACTIONS_PAGE, description="How many"),
    principal: Principal = Depends(rm_gate),
    session: Session = Depends(get_session),
) -> InteractionListResponse:
    return service.list_interactions(session, _scope(principal), customer_id, limit)


@router.post(
    "/customers/{customer_id}/interactions",
    response_model=InteractionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Log a call, meeting, email or note about a customer (audited)",
    responses={404: {"description": "No such customer, or not assigned to the calling RM"}},
)
def log_customer_interaction(
    customer_id: UUID,
    body: InteractionCreate,
    principal: Principal = Depends(rm_gate),
    session: Session = Depends(get_session),
    meta: RequestMeta = Depends(get_request_meta),
) -> InteractionOut:
    return service.log_interaction(
        session,
        _scope(principal),
        customer_id,
        body,
        ip=meta.ip,
        request_id=meta.request_id,
    )


@router.get(
    "/relationship-managers",
    response_model=RelationshipManagerListResponse,
    summary="Active relationship managers, for assigning customers (ADMIN with MFA)",
)
def list_relationship_managers(
    _admin: Principal = Depends(assignment_gate),
    session: Session = Depends(get_session),
) -> RelationshipManagerListResponse:
    return service.list_relationship_managers(session)


@router.put(
    "/customers/{customer_id}/assignment",
    response_model=RmAssignmentOut,
    summary="Assign or reassign a customer to a relationship manager (ADMIN with MFA, audited)",
    responses={
        404: {"description": "No such customer"},
        422: {"description": "rm_user_id is not an active relationship manager"},
    },
)
def assign_relationship_manager(
    customer_id: UUID,
    body: RmAssignmentRequest,
    admin: Principal = Depends(assignment_gate),
    session: Session = Depends(get_session),
    meta: RequestMeta = Depends(get_request_meta),
    clock: Callable[[], float] = Depends(get_clock),
) -> RmAssignmentOut:
    return service.assign_rm(
        session,
        customer_id,
        body.rm_user_id,
        admin.user_id,
        now=_now(clock),
        ip=meta.ip,
        request_id=meta.request_id,
    )


@router.delete(
    "/customers/{customer_id}/assignment",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="End a customer's relationship-manager assignment (ADMIN with MFA, audited)",
    responses={404: {"description": "No such customer"}},
)
def unassign_relationship_manager(
    customer_id: UUID,
    admin: Principal = Depends(assignment_gate),
    session: Session = Depends(get_session),
    meta: RequestMeta = Depends(get_request_meta),
    clock: Callable[[], float] = Depends(get_clock),
) -> None:
    service.unassign_rm(
        session,
        customer_id,
        admin.user_id,
        now=_now(clock),
        ip=meta.ip,
        request_id=meta.request_id,
    )
