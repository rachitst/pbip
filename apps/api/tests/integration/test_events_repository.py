"""The clickstream batch insert at the repository level (TASK-055, issue #78).

The insert became one fixed ``INSERT ... SELECT FROM unnest(...)`` statement for speed
(docs/testing/events-latency-2026-11-04.md). These tests pin what must not change with it: every
column is stored as sent, each event lands in its monthly partition, a repeated event is skipped
both inside one batch and across batches, and the statement is the same for every batch size, so
SQLAlchemy compiles it once. No test measures time.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from pbip_api.core.db import utcnow
from pbip_api.modules.events import repository
from pbip_api.modules.events.models import ClickstreamEvent
from pbip_test_support import Actor
from sqlalchemy import Engine, event, func, select, text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


def _record(customer_id: UUID | None, **overrides: Any) -> dict[str, Any]:
    now = utcnow()
    record: dict[str, Any] = {
        "id": uuid4(),
        "event_timestamp": now,
        "event_type": "page_view",
        "customer_id": customer_id,
        "session_id": f"sess-{uuid4().hex[:8]}",
        "page_url": "/accounts/:id",
        "properties": {},
        "created_at": now,
    }
    record.update(overrides)
    return record


def test_every_column_is_stored_as_sent(db: Session, make_customer: Callable[..., Actor]) -> None:
    alice = make_customer()
    full = _record(
        alice.customer_id,
        event_type="loan_calculator_used",
        properties={"tenure_months": 36, "amount_band": "1L_5L", "emi_shown": True, "rate": 10.5},
    )
    bare = _record(None, page_url=None)

    assert repository.insert_events_batch(db, [full, bare]) == 2
    db.commit()

    stored = {
        row.id: row
        for row in db.scalars(
            select(ClickstreamEvent).where(ClickstreamEvent.id.in_([full["id"], bare["id"]]))
        )
    }
    for record in (full, bare):
        row = stored[record["id"]]
        assert row.event_timestamp == record["event_timestamp"]
        assert row.event_type == record["event_type"]
        assert row.customer_id == record["customer_id"]
        assert row.session_id == record["session_id"]
        assert row.page_url == record["page_url"]
        assert row.properties == record["properties"]
        assert row.created_at == record["created_at"]


def test_each_event_lands_in_its_monthly_partition(db: Session) -> None:
    now = datetime.now(UTC)
    last_month = now.replace(day=1) - timedelta(days=1)
    this_one, previous = (
        _record(None, event_timestamp=now),
        _record(None, event_timestamp=last_month),
    )

    assert repository.insert_events_batch(db, [this_one, previous]) == 2
    db.commit()

    partitions: dict[UUID, str] = dict(
        db.execute(
            text(
                "SELECT id, tableoid::regclass::text FROM clickstream_events WHERE id IN (:a, :b)"
            ),
            {"a": this_one["id"], "b": previous["id"]},
        ).all()
    )
    assert partitions == {
        this_one["id"]: f"clickstream_events_{now:%Y_%m}",
        previous["id"]: f"clickstream_events_{last_month:%Y_%m}",
    }


def test_a_repeated_event_is_skipped_inside_a_batch_and_across_batches(db: Session) -> None:
    stored = _record(None)
    assert repository.insert_events_batch(db, [stored]) == 1
    db.commit()

    fresh = [_record(None) for _ in range(97)]
    batch = [stored, *fresh, fresh[0], dict(fresh[1])]  # 100 events, 3 already seen
    assert repository.insert_events_batch(db, batch) == 97
    db.commit()

    ids = [stored["id"], *(record["id"] for record in fresh)]
    count = db.execute(
        select(func.count()).select_from(ClickstreamEvent).where(ClickstreamEvent.id.in_(ids))
    ).scalar_one()
    assert count == 98


def test_an_empty_batch_runs_no_statement(db: Session, migrated_engine: Engine) -> None:
    with _captured_statements(migrated_engine) as statements:
        assert repository.insert_events_batch(db, []) == 0
    assert statements == []


def test_one_fixed_statement_whatever_the_batch_size(db: Session, migrated_engine: Engine) -> None:
    """Regression: ``insert().values(records)`` built a new statement per batch size (8 x N bind
    parameters, recompiled on every call), the dominant cost of a 100-event request."""
    with _captured_statements(migrated_engine) as statements:
        for size in (1, 10, 100):
            assert repository.insert_events_batch(db, [_record(None) for _ in range(size)]) == size
        db.commit()
    inserts = [sql for sql in statements if sql.lstrip().upper().startswith("INSERT")]
    assert len(inserts) == 3  # one execution per batch, not one per event
    assert len(set(inserts)) == 1
    assert "unnest(" in inserts[0]


@contextmanager
def _captured_statements(engine: Engine) -> Iterator[list[str]]:
    statements: list[str] = []

    def listen(_conn: object, _cursor: object, statement: str, *_rest: object) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", listen)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", listen)
