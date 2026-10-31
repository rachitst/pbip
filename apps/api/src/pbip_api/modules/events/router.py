"""FastAPI router for telemetry and clickstream event ingestion (TASK-055)."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from pbip_api.core.db import get_session
from pbip_api.core.ratelimit import rate_limit
from pbip_api.modules.customers.dependencies import get_current_customer
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.events import service
from pbip_api.modules.events.schemas import EventBatchRequest, EventBatchResponse

router = APIRouter(prefix="/api/v1/events", tags=["events"])

events_limiter = rate_limit("events", lambda s: s.rate_limit_events_per_minute, 60)


@router.post(
    "/batch",
    response_model=EventBatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Batch ingest client-side and server-side telemetry events",
)
def ingest_events_batch(
    payload: EventBatchRequest,
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
    _limited: None = Depends(events_limiter),
) -> EventBatchResponse:
    """Store up to 100 events for the authenticated customer; committed before the response."""
    return service.ingest_event_batch(session=session, request=payload, customer_id=customer.id)
