"""Database repository for clickstream and telemetry events (TASK-055)."""

from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from pbip_api.modules.events.models import ClickstreamEvent


def insert_events_batch(session: Session, records: list[dict[str, Any]]) -> int:
    """Bulk insert; rows whose (id, event_timestamp) already exist are skipped. Returns inserted."""
    if not records:
        return 0
    stmt = (
        insert(ClickstreamEvent)
        .values(records)
        .on_conflict_do_nothing(index_elements=["id", "event_timestamp"])
        .returning(ClickstreamEvent.id)
    )
    return len(session.execute(stmt).all())
