"""Database repository for clickstream and telemetry events (TASK-055)."""

from typing import Any
from uuid import UUID

from sqlalchemy import desc, insert, select
from sqlalchemy.orm import Session

from pbip_api.modules.events.models import ClickstreamEvent


def insert_events_batch(
    session: Session,
    records: list[dict[str, Any]],
) -> int:
    """Perform high-throughput bulk insertion of clickstream event records."""
    if not records:
        return 0

    stmt = insert(ClickstreamEvent)
    session.execute(stmt, records)
    return len(records)


def list_events_by_customer(
    session: Session,
    customer_id: UUID,
    limit: int = 50,
) -> list[ClickstreamEvent]:
    """Retrieve recent clickstream events for a specific customer."""
    stmt = (
        select(ClickstreamEvent)
        .where(ClickstreamEvent.customer_id == customer_id)
        .order_by(desc(ClickstreamEvent.event_timestamp))
        .limit(limit)
    )
    return list(session.scalars(stmt).all())


def list_events_by_session(
    session: Session,
    session_id: str,
    limit: int = 50,
) -> list[ClickstreamEvent]:
    """Retrieve recent clickstream events for a specific session."""
    stmt = (
        select(ClickstreamEvent)
        .where(ClickstreamEvent.session_id == session_id)
        .order_by(desc(ClickstreamEvent.event_timestamp))
        .limit(limit)
    )
    return list(session.scalars(stmt).all())
