"""FastAPI router for telemetry and clickstream event ingestion (TASK-055)."""

import json

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from pbip_api.core.db import get_session, utcnow
from pbip_api.core.ratelimit import rate_limit
from pbip_api.core.redis import get_redis
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


# DIAGNOSTIC ONLY (branch diag/events-async-floor, never merged): the floor of any asynchronous
# design for #78. ``noop`` does auth, rate limit and validation and stores nothing; ``redis`` adds
# one durable XADD of the batch (the workflow sets appendfsync always first).
@router.post(
    "/batch-noop-diag",
    response_model=EventBatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Diagnostic: validate only",
    include_in_schema=False,
)
def diag_noop(
    payload: EventBatchRequest,
    customer: Customer = Depends(get_current_customer),
    _limited: None = Depends(events_limiter),
) -> EventBatchResponse:
    """Diagnostic only."""
    return EventBatchResponse(accepted=len(payload.events), duplicates=0, received_at=utcnow())


@router.post(
    "/batch-redis-diag",
    response_model=EventBatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Diagnostic: durable Redis enqueue",
    include_in_schema=False,
)
def diag_redis(
    payload: EventBatchRequest,
    customer: Customer = Depends(get_current_customer),
    _limited: None = Depends(events_limiter),
) -> EventBatchResponse:
    """Diagnostic only."""
    body = json.dumps(
        {"customer_id": str(customer.id), "events": payload.model_dump(mode="json")["events"]}
    )
    get_redis().xadd("pbip:diag:events", {"b": body})
    return EventBatchResponse(accepted=len(payload.events), duplicates=0, received_at=utcnow())
