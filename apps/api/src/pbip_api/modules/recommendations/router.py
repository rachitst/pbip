"""FastAPI router for customer personalized product recommendations (TASK-054)."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from pbip_api.core.db import get_session
from pbip_api.modules.customers.dependencies import get_current_customer
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.recommendations import service
from pbip_api.modules.recommendations.schemas import RecommendationsResponse

router = APIRouter(prefix="/api/v1/recommendations", tags=["recommendations"])


@router.get(
    "",
    response_model=RecommendationsResponse,
    status_code=status.HTTP_200_OK,
    summary="Personalized banking product recommendations with explainable reasons",
)
def get_recommendations(
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
) -> RecommendationsResponse:
    """Top 3 eligible products for the authenticated customer, with reasons from their data."""
    return service.get_customer_recommendations(
        session=session,
        customer_id=customer.id,
    )
