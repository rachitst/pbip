"""Service layer for clickstream and telemetry ingestion (TASK-055).

Events are inserted and committed inside the request, before the 202 response: nothing is queued in
memory, so a failure surfaces as an error the client can retry instead of being lost silently.
Retries are safe: an event resent with the same ``event_id`` and timestamp is skipped.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from pbip_api.core.db import utcnow
from pbip_api.modules.events import repository
from pbip_api.modules.events.schemas import EventBatchRequest, EventBatchResponse


def ingest_event_batch(
    session: Session,
    request: EventBatchRequest,
    customer_id: UUID,
    now: datetime | None = None,
) -> EventBatchResponse:
    """Persist a validated batch for the authenticated customer (never a client-sent id)."""
    receipt_time = now or utcnow()
    records = [
        {
            "id": event.event_id or uuid4(),
            "event_timestamp": event.event_timestamp,
            "event_type": event.event_type.value,
            "customer_id": customer_id,
            "session_id": event.session_id,
            "page_url": event.page_url,
            "properties": event.properties,
            "created_at": receipt_time,
        }
        for event in request.events
    ]
    inserted = repository.insert_events_batch(session=session, records=records)
    session.commit()
    return EventBatchResponse(
        accepted=inserted,
        duplicates=len(records) - inserted,
        batch_id=uuid4(),
        received_at=receipt_time,
    )
