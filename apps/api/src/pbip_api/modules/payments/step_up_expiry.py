"""Expiry of unanswered step-up challenges (issue #314, ADR-0036).

A transfer in ``PENDING_STEP_UP`` waits for the customer's second factor. Nothing is held while it
waits (the money moves only when a correct code arrives), but it counts against the day's limit and
carries a fraud decision that grows stale. After ``Settings.step_up_expiry_minutes`` the beat job
declines it (``decline_reason = STEP_UP_EXPIRED``), tells the customer and audits it.

Each transfer is ONE use case in ONE database transaction, in the AGENTS.md lock order: the
customer's advisory lock (the same lock ``confirm_step_up`` takes first), then the transaction row
``FOR UPDATE``, re-checked. A step-up confirmation racing the job is therefore serialised: whoever
is second sees the other's result (job: ``SKIPPED``; customer: 409). No ledger call is needed or
made: a ``PENDING_STEP_UP`` transfer has no hold and no entries.
"""

import time
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

import structlog
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import get_engine, utcnow
from pbip_api.core.metrics import TRANSFERS_TOTAL
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.ledger.models import Transaction, TransactionStatus
from pbip_api.modules.notifications import service as notifications

logger = structlog.get_logger(__name__)

DECLINE_REASON = "STEP_UP_EXPIRED"


class ExpiryOutcome(StrEnum):
    EXPIRED = "EXPIRED"
    SKIPPED = "SKIPPED"  # answered, escalated or not yet due by the time the lock was taken
    ERROR = "ERROR"  # unexpected failure, rolled back and logged; retried next run


def expiry_cutoff(now: datetime, settings: Settings) -> datetime:
    """Transfers created at or before this instant have waited too long for their code."""
    return now - timedelta(minutes=settings.step_up_expiry_minutes)


def stale_step_ups(session: Session, now: datetime, settings: Settings) -> list[tuple[UUID, UUID]]:
    """(transaction id, customer user id) of overdue step-ups, oldest first, one batch."""
    rows = session.execute(
        select(Transaction.id, Transaction.initiated_by_user_id)
        .where(
            Transaction.status == TransactionStatus.PENDING_STEP_UP,
            Transaction.created_at <= expiry_cutoff(now, settings),
            Transaction.initiated_by_user_id.is_not(None),
        )
        .order_by(Transaction.created_at, Transaction.id)
        .limit(settings.step_up_expiry_batch_size)
    ).all()
    return [(row[0], row[1]) for row in rows if row[1] is not None]


def expire_step_up(
    session: Session, settings: Settings, transaction_id: UUID, user_id: UUID, now: datetime
) -> ExpiryOutcome:
    """Decline one overdue step-up, at most once. The caller's session must hold no transaction."""
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:u))"), {"u": str(user_id)})
    transaction = session.execute(
        select(Transaction)
        .where(Transaction.id == transaction_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if (
        transaction is None
        or transaction.status != TransactionStatus.PENDING_STEP_UP
        or transaction.created_at > expiry_cutoff(now, settings)
    ):
        session.rollback()
        return ExpiryOutcome.SKIPPED
    transaction.status = TransactionStatus.DECLINED
    transaction.decline_reason = DECLINE_REASON
    notifications.enqueue(
        session,
        notifications.TRANSFER_DECLINED,
        {
            "user_id": str(user_id),
            "transaction_id": str(transaction.id),
            "account_id": str(transaction.debit_account_id),
            "amount_minor": transaction.amount,
            "currency": transaction.currency,
        },
    )
    AuditService(session).record(
        None,
        "TRANSFER_STEP_UP_EXPIRED",
        "transaction",
        str(transaction.id),
        details={
            "actor": "scheduler",
            "reason": DECLINE_REASON,
            "timeout_minutes": settings.step_up_expiry_minutes,
            "amount_minor": transaction.amount,
        },
    )
    session.commit()
    TRANSFERS_TOTAL.labels(TransactionStatus.DECLINED.value).inc()
    return ExpiryOutcome.EXPIRED


def expire_stale_step_ups(
    engine: Engine | None = None,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> dict[str, int]:
    """The beat job: each transfer in its own session and transaction, so one failure never rolls
    back or blocks another. ``now`` is injected by tests; the task passes the wall clock."""
    started = time.monotonic()
    db_engine = engine or get_engine()
    config = settings or get_settings()
    moment = now or utcnow()
    counts = {outcome.value: 0 for outcome in ExpiryOutcome}
    with Session(db_engine) as session:
        due = stale_step_ups(session, moment, config)
    for transaction_id, user_id in due:
        with Session(db_engine, expire_on_commit=False) as session:
            try:
                outcome = expire_step_up(session, config, transaction_id, user_id, moment)
            except Exception:  # one transfer must never stop the batch; retried next run
                logger.exception("step_up_expiry_failed", transaction_id=str(transaction_id))
                session.rollback()
                outcome = ExpiryOutcome.ERROR
        counts[outcome.value] += 1
    logger.info(
        "step_up_expiry_run",
        due=len(due),
        duration_ms=round((time.monotonic() - started) * 1000),
        **counts,
    )
    return counts
