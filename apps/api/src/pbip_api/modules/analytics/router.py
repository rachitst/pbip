"""HTTP router for customer analytics, spending breakdowns, and trends."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from pbip_api.core.db import get_session
from pbip_api.modules.analytics import service
from pbip_api.modules.analytics.schemas import (
    BalanceForecastResponse,
    CustomerSegmentResponse,
    FinancialHealthScoreResponse,
    SpendingBreakdownResponse,
    SpendingTrendsResponse,
)
from pbip_api.modules.auth.dependencies import get_clock
from pbip_api.modules.customers.dependencies import get_current_customer
from pbip_api.modules.customers.models import Customer

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


@router.get(
    "/spending",
    response_model=SpendingBreakdownResponse,
    status_code=status.HTTP_200_OK,
    summary="Customer spending breakdown across categories",
)
def get_customer_spending(
    account_id: UUID = Query(..., description="Target account identifier"),
    year: int | None = Query(None, ge=2000, le=2200, description="Calendar year (e.g. 2026)"),
    month: int | None = Query(None, ge=1, le=12, description="Calendar month (1-12)"),
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
) -> SpendingBreakdownResponse:
    """Return categorized spending breakdown for an authenticated customer's account."""
    return service.get_spending_breakdown(
        session=session,
        customer_id=customer.id,
        account_id=account_id,
        year=year,
        month=month,
    )


@router.get(
    "/trends",
    response_model=SpendingTrendsResponse,
    status_code=status.HTTP_200_OK,
    summary="Month-over-month spending trends and daily burn rate",
)
def get_customer_trends(
    account_id: UUID = Query(..., description="Target account identifier"),
    year: int | None = Query(None, ge=2000, le=2200, description="Calendar year (e.g. 2026)"),
    month: int | None = Query(None, ge=1, le=12, description="Calendar month (1-12)"),
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
) -> SpendingTrendsResponse:
    """Return month-over-month spending trends and burn rate comparison."""
    return service.get_spending_trends(
        session=session,
        customer_id=customer.id,
        account_id=account_id,
        year=year,
        month=month,
    )


@router.get(
    "/health-score",
    response_model=FinancialHealthScoreResponse,
    status_code=status.HTTP_200_OK,
    summary="Financial health score (0-100) with explainable components and tips",
)
def get_customer_health_score(
    account_id: UUID = Query(..., description="Target account identifier"),
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
    clock: Callable[[], float] = Depends(get_clock),
) -> FinancialHealthScoreResponse:
    """Savings rate, expense volatility, loan repayments to income, cash buffer (owned account)."""
    return service.get_financial_health_score(
        session=session,
        customer_id=customer.id,
        account_id=account_id,
        now=datetime.fromtimestamp(clock(), tz=UTC),
    )


@router.get(
    "/segment",
    response_model=CustomerSegmentResponse,
    status_code=status.HTTP_200_OK,
    summary="Behavioural segment assigned to the customer by the offline job (404 until it ran)",
)
def get_customer_segment(
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
) -> CustomerSegmentResponse:
    """Segment, confidence and the RFM features it was derived from; the principal's own only."""
    return service.get_customer_segment(session=session, customer_id=customer.id)


@router.get(
    "/forecast",
    response_model=BalanceForecastResponse,
    status_code=status.HTTP_200_OK,
    summary="Projected end-of-day balance with 80% and 95% intervals (Predicted, not exact)",
)
def get_balance_forecast(
    account_id: UUID = Query(..., description="Target account identifier"),
    horizon_days: int = Query(30, ge=7, le=90, description="Days ahead to project"),
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
    clock: Callable[[], float] = Depends(get_clock),
) -> BalanceForecastResponse:
    """Daily balance projection of an owned account. Predicted, not exact."""
    return service.get_balance_forecast(
        session=session,
        customer_id=customer.id,
        account_id=account_id,
        horizon_days=horizon_days,
        now=datetime.fromtimestamp(clock(), tz=UTC),
    )
