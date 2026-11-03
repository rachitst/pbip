"""Daily purge of expired idempotency keys (issue #314, ADR-0036).

An expired key is already ignored by every ``_begin_idempotent`` (it counts as unused), so the purge
changes no behaviour: it only stops ``idempotency_keys`` from growing forever. Each batch is its own
short transaction, so the job never holds many row locks or one long transaction.
"""

import time
from datetime import datetime

import structlog
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import get_engine, utcnow
from pbip_api.modules.ledger import service as ledger

logger = structlog.get_logger(__name__)

# One run deletes at most this many batches; anything left goes on the next day's run.
MAX_BATCHES_PER_RUN = 100


def purge_expired_keys(
    engine: Engine | None = None,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> int:
    """Delete keys that expired at or before ``now`` and return how many were deleted."""
    started = time.monotonic()
    db_engine = engine or get_engine()
    config = settings or get_settings()
    moment = now or utcnow()
    deleted = 0
    for _ in range(MAX_BATCHES_PER_RUN):
        with Session(db_engine) as session:
            batch = ledger.purge_expired_idempotency_keys(
                session, moment, config.idempotency_purge_batch_size
            )
            session.commit()
        deleted += batch
        if batch < config.idempotency_purge_batch_size:
            break
    logger.info(
        "idempotency_key_purge_run",
        deleted=deleted,
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return deleted
