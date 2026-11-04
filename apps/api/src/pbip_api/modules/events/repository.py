"""Database repository for clickstream and telemetry events (TASK-055)."""

from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, String, bindparam, delete, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from pbip_api.modules.events.models import ClickstreamEvent

# One INSERT ... SELECT FROM unnest(...) per batch: a fixed statement with one array parameter per
# column, so SQLAlchemy compiles it once and caches it. The previous ``insert().values(records)``
# compiled a new statement with 8 x N parameters on every call, which made a 100-event insert
# several times slower than the database's own work (docs/testing/events-latency-2026-11-04.md).
# ON CONFLICT DO NOTHING still skips an event repeated inside the same batch, as before.
_COLUMNS = (
    ("id", ARRAY(PG_UUID(as_uuid=True))),
    ("event_timestamp", ARRAY(DateTime(timezone=True))),
    ("event_type", ARRAY(String)),
    ("customer_id", ARRAY(PG_UUID(as_uuid=True))),
    ("session_id", ARRAY(String)),
    ("page_url", ARRAY(String)),
    ("properties", ARRAY(JSONB)),
    ("created_at", ARRAY(DateTime(timezone=True))),
)
_INSERT_BATCH = text(
    "INSERT INTO clickstream_events"
    " (id, event_timestamp, event_type, customer_id, session_id, page_url, properties, created_at)"
    " SELECT * FROM unnest(:id, :event_timestamp, :event_type, :customer_id, :session_id,"
    " :page_url, :properties, :created_at)"
    " ON CONFLICT (id, event_timestamp) DO NOTHING RETURNING id"
).bindparams(*(bindparam(name, type_=type_) for name, type_ in _COLUMNS))


def insert_events_batch(session: Session, records: list[dict[str, Any]]) -> int:
    """Bulk insert; rows whose (id, event_timestamp) already exist are skipped. Returns inserted."""
    if not records:
        return 0
    params = {name: [record[name] for record in records] for name, _ in _COLUMNS}
    return len(session.execute(_INSERT_BATCH, params).all())


def delete_customer_events(session: Session, customer_id: UUID) -> int:
    """Delete all of one customer's events (every partition; the customer index finds them)."""
    result = session.execute(
        delete(ClickstreamEvent).where(ClickstreamEvent.customer_id == customer_id)
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]  # DELETE results have rowcount
