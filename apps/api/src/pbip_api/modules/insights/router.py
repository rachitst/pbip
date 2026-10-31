"""FastAPI router for Relationship Manager behavioral insights (TASK-056)."""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from pbip_api.core.db import get_session
from pbip_api.modules.auth.dependencies import Principal, require_roles
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.insights import service
from pbip_api.modules.insights.schemas import CustomerInsightsListResponse

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
    )
