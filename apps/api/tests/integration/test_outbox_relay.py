"""TASK-026 / ADR-0040: outbox relay retries, dead letters, purge, and exactly-once delivery.

Events are written with a fixed ``created_at``/``next_attempt_at`` and the relay is given ``now``,
so every schedule is asserted exactly and nothing sleeps.
"""

import os
import signal
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
from pbip_api.core.config import Settings
from pbip_api.core.db import session_scope, utcnow
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.notifications import outbox_purge
from pbip_api.modules.notifications import service as notifications
from pbip_api.modules.notifications.models import Notification, OutboxEvent
from pbip_api.modules.notifications.service import RetryPolicy, relay_outbox
from pbip_test_support import Actor
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _event(
    db: Session,
    user_id: uuid.UUID,
    *,
    event_type: str = notifications.TRANSFER_POSTED,
    payload: dict[str, Any] | None = None,
    at: datetime = T0,
) -> uuid.UUID:
    body = (
        {"user_id": str(user_id), "amount_minor": 500, "currency": "INR"}
        if payload is None
        else payload
    )
    event = OutboxEvent(event_type=event_type, payload=body, created_at=at, next_attempt_at=at)
    db.add(event)
    db.flush()
    return event.id


def _reload(engine: Engine, event_id: uuid.UUID) -> OutboxEvent:
    with Session(engine) as session:
        event = session.get(OutboxEvent, event_id)
        assert event is not None
        session.expunge(event)
        return event


def _delivered(engine: Engine) -> list[uuid.UUID | None]:
    with Session(engine) as session:
        return list(session.scalars(select(Notification.outbox_event_id)).all())


def test_failing_event_is_retried_with_backoff_and_does_not_block_the_batch(
    db: Session, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    alice = make_customer()
    # Oldest first, so the poison event is at the head of the batch.
    bad = _event(db, alice.user_id, payload={"user_id": str(alice.user_id)}, at=T0)
    good = _event(db, alice.user_id, at=T0 + timedelta(seconds=1))
    db.commit()
    policy = RetryPolicy(max_attempts=4, base_seconds=30, max_seconds=3600)

    assert relay_outbox(db, now=T0 + timedelta(seconds=1), policy=policy) == 2
    db.commit()
    assert _delivered(migrated_engine) == [good]
    failed = _reload(migrated_engine, bad)
    assert (failed.attempts, failed.last_error, failed.processed_at) == (1, "KeyError", None)
    assert failed.next_attempt_at == T0 + timedelta(seconds=31)

    # Not due yet: skipped, attempts unchanged.
    assert relay_outbox(db, now=T0 + timedelta(seconds=30), policy=policy) == 0
    db.commit()
    assert _reload(migrated_engine, bad).attempts == 1

    # Due: fails again and the wait doubles (30 s, then 60 s).
    assert relay_outbox(db, now=T0 + timedelta(seconds=31), policy=policy) == 1
    db.commit()
    failed = _reload(migrated_engine, bad)
    assert failed.attempts == 2
    assert failed.next_attempt_at == T0 + timedelta(seconds=31 + 60)
    assert _delivered(migrated_engine) == [good]


def test_event_is_dead_lettered_after_the_last_attempt_and_audited(
    db: Session, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    alice = make_customer()
    unknown = _event(db, alice.user_id, event_type="NOT_A_HANDLED_TYPE")
    db.commit()
    policy = RetryPolicy(max_attempts=3, base_seconds=10, max_seconds=15)

    moment = T0
    waits = []
    with structlog.testing.capture_logs() as logs:
        for _ in range(policy.max_attempts):
            assert relay_outbox(db, now=moment, policy=policy) == 1
            db.commit()
            event = _reload(migrated_engine, unknown)
            waits.append(event.next_attempt_at - moment)
            moment = event.next_attempt_at
    # 10 s, then 20 s capped at 15 s; the last failure dead-letters instead of scheduling.
    assert waits[:2] == [timedelta(seconds=10), timedelta(seconds=15)]
    event = _reload(migrated_engine, unknown)
    assert (event.attempts, event.last_error) == (3, "NoOutboxHandlerError")
    assert event.dead_lettered_at == T0 + timedelta(seconds=25)
    assert event.processed_at is None

    # Dead letters are never picked up again, however late the relay runs.
    assert relay_outbox(db, now=T0 + timedelta(days=365), policy=policy) == 0
    db.commit()
    assert _delivered(migrated_engine) == []

    (audit,) = db.scalars(
        select(AuditLog).where(AuditLog.action == "OUTBOX_EVENT_DEAD_LETTERED")
    ).all()
    assert (audit.actor_user_id, audit.resource_type, audit.resource_id) == (
        None,
        "outbox_event",
        str(unknown),
    )
    assert audit.details == {
        "actor": "scheduler",
        "event_type": "NOT_A_HANDLED_TYPE",
        "attempts": 3,
        "error": "NoOutboxHandlerError",
    }
    assert AuditService(db).verify_chain().valid
    events = [entry["event"] for entry in logs]
    assert events.count("outbox_delivery_retry_scheduled") == 2
    assert events.count("outbox_event_dead_lettered") == 1


class _WorkerKilledError(BaseException):
    """Stands in for SystemExit/KeyboardInterrupt: not an ``Exception``, so no handler catches it."""


def test_crash_after_some_handlers_ran_loses_and_duplicates_nothing(
    db: Session, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    alice = make_customer()
    ids = [_event(db, alice.user_id, at=T0 + timedelta(seconds=i)) for i in range(3)]
    db.commit()
    calls: list[uuid.UUID] = []

    def dies_on_third(session: Session, event: OutboxEvent) -> None:
        calls.append(event.id)
        if len(calls) == 3:
            raise _WorkerKilledError
        notifications.OUTBOX_HANDLERS[event.event_type](session, event)

    crashing = dict.fromkeys(notifications.OUTBOX_HANDLERS, dies_on_third)
    with (
        pytest.raises(_WorkerKilledError),
        session_scope(migrated_engine) as session,
    ):
        relay_outbox(session, now=T0 + timedelta(minutes=1), handlers=crashing)
    assert calls == ids  # two deliveries ran before the crash ...
    assert _delivered(migrated_engine) == []  # ... and rolled back with it
    for event_id in ids:
        event = _reload(migrated_engine, event_id)
        assert (event.processed_at, event.attempts) == (None, 0)

    with session_scope(migrated_engine) as session:
        assert relay_outbox(session, now=T0 + timedelta(minutes=2)) == 3
    with session_scope(migrated_engine) as session:
        assert relay_outbox(session, now=T0 + timedelta(minutes=3)) == 0
    assert sorted(map(str, _delivered(migrated_engine))) == sorted(map(str, ids))


_KILLED_WORKER = """
import os, signal, sys
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
import pbip_api.modules  # registers every model
from pbip_api.modules.notifications.models import Notification
from pbip_api.modules.notifications.service import relay_outbox

session = Session(create_engine(os.environ["PBIP_KILLED_WORKER_DB_URL"]))
relayed = relay_outbox(session)
seen = session.scalar(select(func.count()).select_from(Notification))
sys.stdout.write(f"{relayed} {seen}\\n")
sys.stdout.flush()
os.kill(os.getpid(), getattr(signal, "SIGKILL", signal.SIGTERM))
"""


def test_worker_killed_between_dispatch_and_commit_delivers_exactly_once_on_restart(
    db: Session,
    make_customer: Callable[..., Actor],
    migrated_engine: Engine,
    test_database_url: str,
) -> None:
    alice = make_customer()
    ids = [_event(db, alice.user_id, at=T0 + timedelta(seconds=i)) for i in range(5)]
    db.commit()

    env = {**os.environ, "PBIP_KILLED_WORKER_DB_URL": test_database_url}
    child = subprocess.run(  # noqa: S603 - fixed script, this interpreter, no shell
        [sys.executable, "-c", _KILLED_WORKER],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    # The killed worker had dispatched all five inside its transaction, then died uncommitted.
    assert child.stdout.split() == ["5", "5"], child.stderr[-2000:]
    if hasattr(signal, "SIGKILL"):
        assert child.returncode == -signal.SIGKILL
    assert _delivered(migrated_engine) == []
    assert all(_reload(migrated_engine, i).processed_at is None for i in ids)

    with session_scope(migrated_engine) as session:
        assert relay_outbox(session) == 5
    with session_scope(migrated_engine) as session:
        assert relay_outbox(session) == 0
    assert sorted(map(str, _delivered(migrated_engine))) == sorted(map(str, ids))


def test_a_redelivered_event_never_notifies_twice(
    db: Session, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    alice = make_customer()
    event_id = _event(db, alice.user_id)
    db.commit()
    assert relay_outbox(db, now=T0) == 1
    db.commit()

    # Lose the processed mark (a manual re-drive, a restore of an older outbox snapshot).
    db.execute(update(OutboxEvent).values(processed_at=None))
    db.commit()
    assert relay_outbox(db, now=T0 + timedelta(minutes=1)) == 1
    db.commit()
    assert _delivered(migrated_engine) == [event_id]
    assert _reload(migrated_engine, event_id).processed_at == T0 + timedelta(minutes=1)


def test_concurrent_relays_deliver_each_event_exactly_once(
    db: Session, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    alice = make_customer()
    ids = [_event(db, alice.user_id, at=T0 + timedelta(seconds=i)) for i in range(20)]
    db.commit()
    barrier = threading.Barrier(2)
    counts: list[int] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=30)
            with session_scope(migrated_engine) as session:
                counts.append(relay_outbox(session, batch_size=20, now=T0 + timedelta(minutes=1)))
        except BaseException as error:  # surfaced below; a thread cannot fail the test itself
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    assert sum(counts) == 20
    assert sorted(map(str, _delivered(migrated_engine))) == sorted(map(str, ids))


def _processed(db: Session, user_id: uuid.UUID, at: datetime) -> uuid.UUID:
    event_id = _event(db, user_id, at=at - timedelta(seconds=5))
    db.execute(update(OutboxEvent).where(OutboxEvent.id == event_id).values(processed_at=at))
    return event_id


def test_purge_deletes_only_processed_events_past_retention_in_batches(
    db: Session,
    make_customer: Callable[..., Actor],
    migrated_engine: Engine,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alice = make_customer()
    now = datetime(2026, 11, 3, 4, 30, tzinfo=UTC)
    cutoff = now - timedelta(days=30)
    old = [_processed(db, alice.user_id, cutoff - timedelta(days=1, seconds=i)) for i in range(5)]
    at_boundary = _processed(db, alice.user_id, cutoff)
    just_older = _processed(db, alice.user_id, cutoff - timedelta(microseconds=1))
    recent = _processed(db, alice.user_id, now - timedelta(days=1))
    pending = _event(db, alice.user_id, at=now - timedelta(days=90))
    dead = _event(db, alice.user_id, at=now - timedelta(days=90))
    db.execute(
        update(OutboxEvent)
        .where(OutboxEvent.id == dead)
        .values(dead_lettered_at=now - timedelta(days=89), attempts=6)
    )
    db.add(
        Notification(user_id=alice.user_id, type="X", title="t", body="b", outbox_event_id=old[0])
    )
    db.commit()
    config = settings.model_copy(update={"outbox_retention_days": 30, "outbox_purge_batch_size": 2})

    # A run stops after MAX_BATCHES_PER_RUN batches; the rest waits for the next day.
    monkeypatch.setattr(outbox_purge, "MAX_BATCHES_PER_RUN", 2)
    assert outbox_purge.purge_processed_events(migrated_engine, now=now, settings=config) == 4
    monkeypatch.setattr(outbox_purge, "MAX_BATCHES_PER_RUN", 100)
    assert outbox_purge.purge_processed_events(migrated_engine, now=now, settings=config) == 2
    assert outbox_purge.purge_processed_events(migrated_engine, now=now, settings=config) == 0

    with Session(migrated_engine) as session:
        left = set(session.scalars(select(OutboxEvent.id)).all())
        kept_notifications = session.scalar(select(func.count()).select_from(Notification))
    assert left == {at_boundary, recent, pending, dead}
    assert just_older not in left and not left & set(old)
    assert kept_notifications == 1  # the customer's notification outlives its event


def test_a_new_event_is_due_by_the_clock_the_relay_reads(
    db: Session, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    """#371: ``next_attempt_at`` came from the database's ``now()`` (its transaction start, on the
    database host's clock) while the relay compares it with the app's clock. With the database
    clock ahead (Docker Desktop's VM drifts by hundreds of ms) a just-enqueued event was not yet
    due and an immediate relay skipped it. It is now stamped by the app clock at insert."""
    alice = make_customer()
    payload = {"user_id": str(alice.user_id), "amount_minor": 500, "currency": "INR"}
    db.execute(select(1))  # opens the transaction: the database's now() is fixed from here
    before = utcnow()
    notifications.enqueue(db, notifications.TRANSFER_POSTED, payload)
    db.commit()
    after = utcnow()

    (event,) = db.scalars(select(OutboxEvent)).all()
    assert before <= event.next_attempt_at <= after
    assert relay_outbox(db, now=after) == 1
    db.commit()
    assert _delivered(migrated_engine) == [event.id]
