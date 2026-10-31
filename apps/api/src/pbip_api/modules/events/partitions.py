"""Monthly partition maintenance for ``clickstream_events`` (TASK-055).

The migration creates partitions up to March 2027 plus a DEFAULT partition. Without this job every
later event would land in DEFAULT, and PostgreSQL refuses to add a monthly partition for a range
the DEFAULT partition already holds rows for. A daily beat task therefore keeps the current month
and the next ``months_ahead`` months created in advance.

This is the one place DDL runs outside a migration (AGENTS.md section 6): partition maintenance is
operational by nature. Table names and bounds are built from dates only, never from input.
"""

from datetime import date

import structlog
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

_logger = structlog.get_logger(__name__)


def _month_start(year: int, month: int) -> date:
    index = year * 12 + month - 1
    return date(index // 12, index % 12 + 1, 1)


def ensure_monthly_partitions(session: Session, today: date, months_ahead: int = 3) -> list[str]:
    """Create any missing monthly partitions from ``today``'s month onwards. Idempotent.

    Returns the names of the partitions this call created.
    """
    created: list[str] = []
    for offset in range(months_ahead + 1):
        start = _month_start(today.year, today.month + offset)
        end = _month_start(start.year, start.month + 1)
        name = f"clickstream_events_{start:%Y_%m}"
        exists = session.execute(text("SELECT to_regclass(:name)"), {"name": name}).scalar()
        if exists is not None:
            continue
        try:
            with session.begin_nested():
                session.execute(
                    text(
                        f"CREATE TABLE {name} PARTITION OF clickstream_events "  # names from dates
                        f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
                    )
                )
        except DBAPIError as exc:  # e.g. the DEFAULT partition already holds rows for this month
            _logger.error("clickstream_partition_failed", partition=name, error=type(exc).__name__)
            continue
        created.append(name)
        _logger.info("clickstream_partition_created", partition=name)
    return created
