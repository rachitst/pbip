"""Service layer for clickstream and telemetry ingestion (TASK-055)."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from pbip_api.core.db import utcnow
from pbip_api.modules.events import repository
from pbip_api.modules.events.schemas import EventBatchRequest, EventBatchResponse


def ingest_event_batch(
    session: Session,
    request: EventBatchRequest,
    customer_id: UUID | None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> EventBatchResponse:
    """Validate, sanitize, and bulk persist telemetry events."""
    receipt_time = now or utcnow()
    batch_id = uuid4()

    sanitized_user_agent = user_agent[:512] if user_agent else None
    sanitized_ip = ip_address[:64] if ip_address else None

    records = [
        {
            "id": event.event_id or uuid4(),
            "event_timestamp": event.event_timestamp,
            "event_type": event.event_type.value,
            "customer_id": customer_id,
            "session_id": event.session_id,
            "page_url": event.page_url,
            "properties": event.properties,
            "ip_address": sanitized_ip,
            "user_agent": sanitized_user_agent,
            "created_at": receipt_time,
        }
        for event in request.events
    ]

    count = repository.insert_events_batch(session=session, records=records)
    session.commit()

    return EventBatchResponse(
        accepted=count,
        batch_id=batch_id,
        received_at=receipt_time,
    )
