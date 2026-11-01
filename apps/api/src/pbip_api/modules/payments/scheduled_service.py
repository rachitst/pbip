"""Scheduled transfer execution and outage recovery engine (ADR-0021)."""

import time
from datetime import date
from enum import StrEnum
from uuid import UUID

import structlog
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pbip_api.core.config import get_settings
from pbip_api.core.db import get_engine, utcnow
from pbip_api.core.errors import ConflictError, NotFoundError, UnprocessableError
from pbip_api.core.metrics import (
    SCHEDULED_MISSED_OCCURRENCES_TOTAL,
    SCHEDULED_TRANSFERS_TOTAL,
    SCHEDULED_WORKER_DURATION_SECONDS,
)
from pbip_api.core.redis import get_redis
from pbip_api.core.velocity import VelocityCounters
from pbip_api.modules.auth.dependencies import get_field_cipher, get_password_service
from pbip_api.modules.auth.service import AuthService
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.fraud.baseline import BaselineRulesScorer
from pbip_api.modules.ledger.models import TransactionStatus
from pbip_api.modules.ledger.service import AccountNotActiveError, InsufficientFundsError
from pbip_api.modules.notifications import service as notifications
from pbip_api.modules.payments.models import (
    ScheduledTransfer,
    ScheduledTransferStatus,
    SchedulePauseReason,
)
from pbip_api.modules.payments.recurrence import (
    compute_missed_occurrences,
    compute_next_occurrence,
)
from pbip_api.modules.payments.service import (
    LimitExceededError,
    PaymentsService,
    TransferChannel,
    TransferCommand,
)

logger = structlog.get_logger(__name__)

SCHEDULED_LOCK_NAMESPACE = 1346521424  # 0x50424950 (ASCII "PBIP")


class ScheduledExecutionOutcome(StrEnum):
    """Outcome of processing a single scheduled transfer instruction."""

    POSTED = "POSTED"
    REVIEW = "REVIEW"
    BLOCK = "BLOCK"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"
    RECOVERED = "RECOVERED"
    SKIPPED_LOCKED = "SKIPPED_LOCKED"
    SKIPPED_INACTIVE = "SKIPPED_INACTIVE"
    REPLAY = "REPLAY"


def try_lock_scheduled_instruction(session: Session, instruction_id: UUID) -> bool:
    """Non-blocking advisory transaction lock in PBIP namespace (R9)."""
    result = session.execute(
        text("SELECT pg_try_advisory_xact_lock(:ns, hashtext(:id))"),
        {"ns": SCHEDULED_LOCK_NAMESPACE, "id": str(instruction_id)},
    ).scalar()
    return bool(result)


def lock_user_xact(session: Session, user_id: UUID) -> None:
    """Serialise user's money movements across the system (lock order contract)."""
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:u))"),
        {"u": str(user_id)},
    )


def _build_default_payments_service(session: Session) -> PaymentsService:
    settings = get_settings()
    passwords = get_password_service(settings)
    cipher = get_field_cipher(settings)
    try:
        counters: VelocityCounters | None = VelocityCounters(get_redis())
    except Exception:
        counters = None
    auth = AuthService(session, settings, passwords, cipher, velocity=counters)
    scorer = BaselineRulesScorer(session, counters)
    return PaymentsService(session, settings, scorer, auth, counters=counters)


def _recover_missed_occurrences(
    session: Session,
    instruction: ScheduledTransfer,
    now_date: date,
) -> ScheduledExecutionOutcome:
    """Recover expired occurrences in one atomic transaction (ADR-0021 Approach D)."""
    audit = AuditService(session)
    missed_dates = compute_missed_occurrences(
        instruction.start_date,
        instruction.next_run_date,
        instruction.frequency,
        now_date,
        instruction.end_date,
    )

    for missed_date in missed_dates:
        audit.record(
            actor_user_id=None,
            action="SCHEDULED_TRANSFER_MISSED",
            resource_type="scheduled_transfer",
            resource_id=str(instruction.id),
            details={
                "actor": "scheduler",
                "channel": "SCHEDULED",
                "missed_date": missed_date.isoformat(),
                "reason": "expired_without_execution",
            },
        )
        SCHEDULED_MISSED_OCCURRENCES_TOTAL.inc()

    # Find next eligible date >= now_date
    next_eligible: date | None = instruction.next_run_date
    while next_eligible is not None and next_eligible < now_date:
        next_eligible = compute_next_occurrence(
            instruction.start_date,
            next_eligible,
            instruction.frequency,
            instruction.end_date,
        )

    completed = next_eligible is None or (
        instruction.end_date is not None and next_eligible > instruction.end_date
    )

    if completed:
        instruction.status = ScheduledTransferStatus.COMPLETED
    elif next_eligible is not None:
        instruction.next_run_date = next_eligible

    instruction.failure_count = 0
    instruction.last_failure_reason = None

    if missed_dates:
        next_eligible_iso = next_eligible.isoformat() if next_eligible and not completed else None
        payload = {
            "user_id": str(instruction.user_id),
            "account_id": str(instruction.from_account_id),
            "scheduled_transfer_id": str(instruction.id),
            "amount_minor": instruction.amount,
            "currency": instruction.currency,
            "missed_count": len(missed_dates),
            "first_missed_date": missed_dates[0].isoformat(),
            "last_missed_date": missed_dates[-1].isoformat(),
            "next_eligible_date": next_eligible_iso,
            "completed": completed,
        }
        notifications.enqueue(session, "SCHEDULED_TRANSFER_MISSED", payload)

    session.commit()
    SCHEDULED_TRANSFERS_TOTAL.labels(outcome=ScheduledExecutionOutcome.RECOVERED.value).inc()
    return ScheduledExecutionOutcome.RECOVERED


def _execute_due_occurrence(
    session: Session,
    instruction: ScheduledTransfer,
    now_date: date,
    payments_service: PaymentsService | None = None,
) -> ScheduledExecutionOutcome:
    """Execute a same-day due occurrence (next_run_date == now_date)."""
    audit = AuditService(session)
    svc = payments_service or _build_default_payments_service(session)
    customer = session.get(Customer, instruction.customer_id)
    if customer is None:
        instruction.status = ScheduledTransferStatus.PAUSED
        instruction.pause_reason = SchedulePauseReason.CUSTOMER_ACTION_REQUIRED
        instruction.last_failure_reason = "customer_not_found"
        audit.record(
            actor_user_id=None,
            action="SCHEDULED_TRANSFER_AUTO_PAUSED",
            resource_type="scheduled_transfer",
            resource_id=str(instruction.id),
            details={"actor": "scheduler", "channel": "SCHEDULED", "reason": "customer_not_found"},
        )
        session.commit()
        return ScheduledExecutionOutcome.PERMANENT_FAILURE

    idempotency_key = f"sched-{instruction.id}-{now_date.isoformat()}"
    command = TransferCommand(
        from_account_id=instruction.from_account_id,
        amount_minor=instruction.amount,
        currency=instruction.currency,
        payee_id=instruction.payee_id,
        to_account_id=instruction.to_account_id,
        reference=instruction.reference,
    )

    try:
        execution = svc._execute_transfer(
            customer=customer,
            user_id=instruction.user_id,
            command=command,
            idempotency_key=idempotency_key,
            channel=TransferChannel.SCHEDULED,
            interactive=False,
            meta=None,
            device_id=None,
        )
    except (InsufficientFundsError, LimitExceededError) as exc:
        session.rollback()
        return _handle_transient_failure(session, instruction.id, now_date, exc)
    except (AccountNotActiveError, NotFoundError, UnprocessableError, ConflictError) as exc:
        session.rollback()
        return _handle_permanent_failure(session, instruction.id, exc)

    if execution.replayed:
        session.commit()
        SCHEDULED_TRANSFERS_TOTAL.labels(outcome=ScheduledExecutionOutcome.REPLAY.value).inc()
        return ScheduledExecutionOutcome.REPLAY

    tx_status = execution.transaction.status
    if tx_status in (TransactionStatus.POSTED, TransactionStatus.PENDING_REVIEW):
        next_date = compute_next_occurrence(
            instruction.start_date,
            now_date,
            instruction.frequency,
            instruction.end_date,
        )
        if next_date is None:
            instruction.status = ScheduledTransferStatus.COMPLETED
        else:
            instruction.next_run_date = next_date

        instruction.last_run_date = now_date
        instruction.last_transaction_id = execution.transaction.id
        instruction.failure_count = 0
        instruction.last_failure_reason = None

        session.commit()
        svc.record_committed(execution)

        outcome = (
            ScheduledExecutionOutcome.POSTED
            if tx_status == TransactionStatus.POSTED
            else ScheduledExecutionOutcome.REVIEW
        )
        SCHEDULED_TRANSFERS_TOTAL.labels(outcome=outcome.value).inc()
        return outcome

    # Fraud BLOCK (TransactionStatus.DECLINED)
    instruction.status = ScheduledTransferStatus.PAUSED
    instruction.pause_reason = SchedulePauseReason.FRAUD_BLOCK
    instruction.last_run_date = now_date
    instruction.last_transaction_id = execution.transaction.id

    next_date = compute_next_occurrence(
        instruction.start_date,
        now_date,
        instruction.frequency,
        instruction.end_date,
    )
    if next_date is not None:
        instruction.next_run_date = next_date
    else:
        instruction.status = ScheduledTransferStatus.COMPLETED

    audit.record(
        actor_user_id=None,
        action="SCHEDULED_TRANSFER_AUTO_PAUSED",
        resource_type="scheduled_transfer",
        resource_id=str(instruction.id),
        details={"actor": "scheduler", "channel": "SCHEDULED", "reason": "fraud_block"},
    )
    notifications.enqueue(
        session,
        "SCHEDULED_TRANSFER_PAUSED",
        {
            "user_id": str(instruction.user_id),
            "account_id": str(instruction.from_account_id),
            "scheduled_transfer_id": str(instruction.id),
            "amount_minor": instruction.amount,
            "currency": instruction.currency,
            "reason": "fraud_block",
        },
    )
    session.commit()
    svc.record_committed(execution)
    SCHEDULED_TRANSFERS_TOTAL.labels(outcome=ScheduledExecutionOutcome.BLOCK.value).inc()
    return ScheduledExecutionOutcome.BLOCK


def _handle_transient_failure(
    session: Session,
    instruction_id: UUID,
    now_date: date,
    exc: Exception,
) -> ScheduledExecutionOutcome:
    """Record same-day transient failure (insufficient funds, limit) in separate transaction."""
    if not try_lock_scheduled_instruction(session, instruction_id):
        return ScheduledExecutionOutcome.SKIPPED_LOCKED

    instruction = session.execute(
        select(ScheduledTransfer).where(ScheduledTransfer.id == instruction_id).with_for_update()
    ).scalar_one_or_none()

    if instruction is None or instruction.status != ScheduledTransferStatus.ACTIVE:
        session.commit()
        return ScheduledExecutionOutcome.SKIPPED_INACTIVE

    reason = (
        "insufficient_funds" if isinstance(exc, InsufficientFundsError) else "daily_limit_exceeded"
    )

    instruction.failure_count += 1
    instruction.last_failure_reason = reason

    if instruction.last_failed_occurrence_date != now_date:
        instruction.last_failed_occurrence_date = now_date
        notifications.enqueue(
            session,
            "SCHEDULED_TRANSFER_FAILED",
            {
                "user_id": str(instruction.user_id),
                "account_id": str(instruction.from_account_id),
                "scheduled_transfer_id": str(instruction.id),
                "amount_minor": instruction.amount,
                "currency": instruction.currency,
                "failed_date": now_date.isoformat(),
                "reason": reason,
            },
        )

    AuditService(session).record(
        actor_user_id=None,
        action="SCHEDULED_TRANSFER_EXECUTION_FAILED",
        resource_type="scheduled_transfer",
        resource_id=str(instruction.id),
        details={
            "actor": "scheduler",
            "channel": "SCHEDULED",
            "failed_date": now_date.isoformat(),
            "reason": reason,
            "failure_count": instruction.failure_count,
        },
    )
    session.commit()
    SCHEDULED_TRANSFERS_TOTAL.labels(
        outcome=ScheduledExecutionOutcome.RETRYABLE_FAILURE.value
    ).inc()
    return ScheduledExecutionOutcome.RETRYABLE_FAILURE


def _handle_permanent_failure(
    session: Session,
    instruction_id: UUID,
    exc: Exception,
) -> ScheduledExecutionOutcome:
    """Pause instruction due to permanent invalidation (closed account, invalid payee)."""
    if not try_lock_scheduled_instruction(session, instruction_id):
        return ScheduledExecutionOutcome.SKIPPED_LOCKED

    instruction = session.execute(
        select(ScheduledTransfer).where(ScheduledTransfer.id == instruction_id).with_for_update()
    ).scalar_one_or_none()

    if instruction is None or instruction.status != ScheduledTransferStatus.ACTIVE:
        session.commit()
        return ScheduledExecutionOutcome.SKIPPED_INACTIVE

    reason = getattr(exc, "code", type(exc).__name__)
    instruction.status = ScheduledTransferStatus.PAUSED
    instruction.pause_reason = SchedulePauseReason.CUSTOMER_ACTION_REQUIRED
    instruction.last_failure_reason = str(reason)

    AuditService(session).record(
        actor_user_id=None,
        action="SCHEDULED_TRANSFER_AUTO_PAUSED",
        resource_type="scheduled_transfer",
        resource_id=str(instruction.id),
        details={"actor": "scheduler", "channel": "SCHEDULED", "reason": str(reason)},
    )
    notifications.enqueue(
        session,
        "SCHEDULED_TRANSFER_PAUSED",
        {
            "user_id": str(instruction.user_id),
            "account_id": str(instruction.from_account_id),
            "scheduled_transfer_id": str(instruction.id),
            "amount_minor": instruction.amount,
            "currency": instruction.currency,
            "reason": str(reason),
        },
    )
    session.commit()
    SCHEDULED_TRANSFERS_TOTAL.labels(
        outcome=ScheduledExecutionOutcome.PERMANENT_FAILURE.value
    ).inc()
    return ScheduledExecutionOutcome.PERMANENT_FAILURE


def execute_scheduled_instruction(
    session: Session,
    instruction_id: UUID,
    *,
    today_utc: date | None = None,
    payments_service: PaymentsService | None = None,
) -> ScheduledExecutionOutcome:
    """Process a single scheduled transfer instruction in the provided session.

    Caller owns session management and transaction boundaries.
    """
    now_date = today_utc or utcnow().date()

    if not try_lock_scheduled_instruction(session, instruction_id):
        return ScheduledExecutionOutcome.SKIPPED_LOCKED

    user_row = session.execute(
        select(ScheduledTransfer.user_id).where(ScheduledTransfer.id == instruction_id)
    ).first()
    if user_row is None:
        return ScheduledExecutionOutcome.SKIPPED_INACTIVE

    lock_user_xact(session, user_row[0])

    instruction = session.execute(
        select(ScheduledTransfer).where(ScheduledTransfer.id == instruction_id).with_for_update()
    ).scalar_one_or_none()

    if (
        instruction is None
        or instruction.status != ScheduledTransferStatus.ACTIVE
        or instruction.next_run_date > now_date
    ):
        return ScheduledExecutionOutcome.SKIPPED_INACTIVE

    if instruction.next_run_date < now_date:
        return _recover_missed_occurrences(session, instruction, now_date)

    return _execute_due_occurrence(
        session, instruction, now_date, payments_service=payments_service
    )


def process_due_scheduled_transfers(
    engine: Engine | None = None,
    *,
    today_utc: date | None = None,
    batch_size: int | None = None,
) -> dict[str, int]:
    """Poll and process due scheduled instructions.

    Each instruction runs in its own isolated database transaction/session.
    """
    start_time = time.monotonic()
    db_engine = engine or get_engine()
    settings = get_settings()
    max_batch = batch_size or settings.scheduled_transfers_batch_size
    now_date = today_utc or utcnow().date()

    counts: dict[str, int] = {outcome.value: 0 for outcome in ScheduledExecutionOutcome}

    # Step 1: Query due instruction IDs in deterministic order
    with Session(db_engine, expire_on_commit=False) as session:
        due_ids = (
            session.execute(
                select(ScheduledTransfer.id)
                .where(
                    ScheduledTransfer.status == ScheduledTransferStatus.ACTIVE,
                    ScheduledTransfer.next_run_date <= now_date,
                )
                .order_by(ScheduledTransfer.next_run_date.asc(), ScheduledTransfer.id.asc())
                .limit(max_batch)
            )
            .scalars()
            .all()
        )

    # Step 2: Process each instruction in an isolated transaction
    for inst_id in due_ids:
        with Session(db_engine, expire_on_commit=False) as inst_session:
            try:
                outcome = execute_scheduled_instruction(inst_session, inst_id, today_utc=now_date)
                counts[outcome.value] += 1
            except Exception:
                logger.exception("scheduled_transfer_failed_unhandled", instruction_id=str(inst_id))
                inst_session.rollback()

    duration = time.monotonic() - start_time
    SCHEDULED_WORKER_DURATION_SECONDS.observe(duration)
    return counts
