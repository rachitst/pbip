"""Daily purge of processed outbox events past the retention period (TASK-026, ADR-0040).

An outbox event is a delivery instruction, not a record: the business change lives in its own
tables, security-relevant actions in the hash-chained audit log, and what the customer was told in
``notifications`` (which keeps the event id as its de-duplication key). Once delivered and older
than ``OUTBOX_RETENTION_DAYS``, the event is deleted. Pending and dead-lettered events are kept.
Each batch is its own short transaction, so the job never holds many row locks for long.
"""

import time
from datetime import datetime, timedelta

import structlog
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import get_engine, utcnow
from pbip_api.modules.notifications import service as notifications

logger = structlog.get_logger(__name__)

# One run deletes at most this many batches; anything left goes on the next day's run.
MAX_BATCHES_PER_RUN = 100


def purge_processed_events(
    engine: Engine | None = None,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> int:
    """Delete events processed more than the retention period before ``now``; return the count."""
    started = time.monotonic()
    db_engine = engine or get_engine()
    config = settings or get_settings()
    cutoff = (now or utcnow()) - timedelta(days=config.outbox_retention_days)
    deleted = 0
    for _ in range(MAX_BATCHES_PER_RUN):
        with Session(db_engine) as session:
            batch = notifications.delete_processed_events(
                session, cutoff, config.outbox_purge_batch_size
            )
            session.commit()
        deleted += batch
        if batch < config.outbox_purge_batch_size:
            break
    logger.info(
        "outbox_purge_run",
        deleted=deleted,
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return deleted
