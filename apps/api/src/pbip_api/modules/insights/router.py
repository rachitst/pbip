"""FastAPI router for Relationship Manager behavioral insights (TASK-056, TASK-066)."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from pbip_api.core.db import get_session
from pbip_api.modules.auth.dependencies import Principal, get_request_meta, require_roles
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.insights import service
from pbip_api.modules.insights.schemas import (
    CustomerInsightDetail,
    CustomerInsightsListResponse,
    InteractionCreate,
    InteractionListResponse,
    InteractionOut,
)

router = APIRouter(prefix="/api/v1/insights", tags=["insights"])

_require_rm_or_admin = require_roles(
    RoleName.RELATIONSHIP_MANAGER,
    RoleName.ADMIN,
    mfa=True,
)


@router.get(
    "/customers",
    response_model=CustomerInsightsListResponse,
    status_code=status.HTTP_200_OK,
    summary="Customers ordered by churn risk, with churn signals and the top eligible product",
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
    # Each row on the page runs the recommendation rules, so pages are small.
    limit: int = Query(20, ge=1, le=25, description="Page size"),
    offset: int = Query(0, ge=0, le=100_000, description="Page offset"),
    principal: Principal = Depends(_require_rm_or_admin),
    session: Session = Depends(get_session),
) -> CustomerInsightsListResponse:
    """One page of customers ordered by churn risk (RM or admin with an MFA-verified session)."""
    return service.get_customer_insights(
        session=session,
        min_churn_risk=min_churn_risk,
        segment=segment,
        limit=limit,
        offset=offset,
        min_loan_propensity=min_loan_propensity,
    )


@router.get(
    "/customers/{customer_id}",
    response_model=CustomerInsightDetail,
    summary="One customer's churn signals, health score, 30-day volume and eligible products",
    responses={404: {"description": "No such customer"}},
)
def get_customer_insight_detail(
    customer_id: UUID,
    _principal: Principal = Depends(_require_rm_or_admin),
    session: Session = Depends(get_session),
) -> CustomerInsightDetail:
    """Detail behind the RM flyout (RM or admin with an MFA-verified session)."""
    return service.get_customer_insight_detail(session, customer_id)


@router.get(
    "/customers/{customer_id}/interactions",
    response_model=InteractionListResponse,
    summary="Calls, meetings, emails and notes logged for a customer, newest first",
    responses={404: {"description": "No such customer"}},
)
def list_customer_interactions(
    customer_id: UUID,
    limit: int = Query(20, ge=1, le=service.MAX_INTERACTIONS_PAGE, description="How many"),
    _principal: Principal = Depends(_require_rm_or_admin),
    session: Session = Depends(get_session),
) -> InteractionListResponse:
    return service.list_interactions(session, customer_id, limit)


@router.post(
    "/customers/{customer_id}/interactions",
    response_model=InteractionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Log a call, meeting, email or note about a customer (audited)",
    responses={404: {"description": "No such customer"}},
)
def log_customer_interaction(
    customer_id: UUID,
    body: InteractionCreate,
    principal: Principal = Depends(_require_rm_or_admin),
    session: Session = Depends(get_session),
    meta: RequestMeta = Depends(get_request_meta),
) -> InteractionOut:
    return service.log_interaction(
        session,
        customer_id,
        principal.user_id,
        body,
        ip=meta.ip,
        request_id=meta.request_id,
    )
