"""FastAPI router for telemetry and clickstream event ingestion (TASK-055)."""

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session

from pbip_api.core.db import get_session
from pbip_api.modules.customers.dependencies import get_current_customer
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.events import service
from pbip_api.modules.events.schemas import EventBatchRequest, EventBatchResponse

router = APIRouter(prefix="/api/v1/events", tags=["events"])


@router.post(
    "/batch",
    response_model=EventBatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Batch ingest client-side and server-side telemetry events",
)
def ingest_events_batch(
    payload: EventBatchRequest,
    request: Request,
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
) -> EventBatchResponse:
    """Ingest up to 100 telemetry events with high-throughput database persistence."""
    client_ip: str | None = None
    if request.client:
        client_ip = request.client.host
    user_agent: str | None = request.headers.get("user-agent")

    return service.ingest_event_batch(
        session=session,
        request=payload,
        customer_id=customer.id,
        ip_address=client_ip,
        user_agent=user_agent,
    )
