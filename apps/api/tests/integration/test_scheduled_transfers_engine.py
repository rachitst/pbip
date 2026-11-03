"""PostgreSQL integration tests for scheduled transfer execution engine (TASK-015 / ADR-0023)."""

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any
from unittest.mock import patch

import pytest
from pbip_api.core.config import Settings
from pbip_api.modules.accounts.models import Account, AccountStatus
from pbip_api.modules.auth.dependencies import get_field_cipher, get_password_service
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.auth.service import AuthService, RequestMeta
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.fraud.models import FraudEvent
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import IdempotencyKey, Transaction, TransactionStatus
from pbip_api.modules.notifications.models import Notification, OutboxEvent
from pbip_api.modules.notifications.service import relay_outbox
from pbip_api.modules.payees.models import Payee, PayeeStatus
from pbip_api.modules.payments import scheduled_service
from pbip_api.modules.payments.models import (
    ScheduledTransfer,
    ScheduledTransferStatus,
    ScheduleFrequency,
    SchedulePauseReason,
)
from pbip_api.modules.payments.recurrence import upcoming_occurrences
from pbip_api.modules.payments.scheduled_service import (
    ScheduledExecutionOutcome,
    execute_scheduled_instruction,
    process_due_scheduled_transfers,
    try_lock_scheduled_instruction,
)
from pbip_api.modules.payments.service import PaymentsService
from pbip_api.modules.runtime_config.allowlist import RuntimeSettingKey
from pbip_api.modules.runtime_config.service import RuntimeConfigService
from pbip_ml.fraud.contract import FraudDecision
from pbip_ml.fraud.testing import FixedDecisionScorer
from pbip_test_support import Actor, age_payee
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

AMOUNT = 10_000


@pytest.fixture(autouse=True)
def _worker_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """The worker reads ``get_settings()`` (the process environment); tests use the generated
    test settings instead, so they need no secrets in the environment (CI has none)."""
    monkeypatch.setattr(scheduled_service, "get_settings", lambda: settings)


@pytest.fixture
def pair(
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> tuple[Actor, Actor, dict[str, Any]]:
    alice = make_customer(funds=100_000)
    bob = make_customer(funds=0)
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)  # baseline rules stay quiet
    return alice, bob, payee


def _create_instruction(
    session: Session,
    alice: Actor,
    payee: dict[str, Any],
    *,
    amount: int = AMOUNT,
    frequency: ScheduleFrequency = ScheduleFrequency.DAILY,
    start_date: date = date(2026, 11, 1),
    next_run_date: date = date(2026, 11, 1),
    end_date: date | None = None,
    status: ScheduledTransferStatus = ScheduledTransferStatus.ACTIVE,
) -> ScheduledTransfer:
    instruction = ScheduledTransfer(
        customer_id=alice.customer_id,
        user_id=alice.user_id,
        from_account_id=alice.account_id,
        payee_id=uuid.UUID(str(payee["id"])),
        to_account_id=None,
        amount=amount,
        currency="INR",
        reference="Monthly rent",
        frequency=frequency,
        status=status,
        start_date=start_date,
        next_run_date=next_run_date,
        end_date=end_date,
    )
    session.add(instruction)
    session.commit()
    return instruction


def test_upcoming_occurrences_projection_is_read_only(
    db: Session,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _, payee = pair
    start = date(2026, 11, 1)
    inst = _create_instruction(
        db, alice, payee, frequency=ScheduleFrequency.WEEKLY, start_date=start, next_run_date=start
    )

    # Project 4 weeks ahead
    until = date(2026, 11, 25)
    projections = upcoming_occurrences(db, alice.customer_id, until)

    assert len(projections) == 4
    expected_dates = [
        date(2026, 11, 1),
        date(2026, 11, 8),
        date(2026, 11, 15),
        date(2026, 11, 22),
    ]
    assert [p.occurrence_date for p in projections] == expected_dates
    for p in projections:
        assert p.scheduled_transfer_id == inst.id
        assert p.from_account_id == alice.account_id
        assert p.amount_minor == AMOUNT
        assert p.currency == "INR"

    # Verify no state changes occurred
    db.refresh(inst)
    assert inst.next_run_date == start
    assert inst.status == ScheduledTransferStatus.ACTIVE
    assert inst.last_run_date is None


def test_successful_scheduled_execution_posts_and_advances(
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, bob, payee = pair
    today = date(2026, 11, 1)

    with Session(migrated_engine) as session:
        inst = _create_instruction(session, alice, payee, next_run_date=today)
        inst_id = inst.id

    with Session(migrated_engine) as session:
        outcome = execute_scheduled_instruction(session, inst_id, today_utc=today)
        assert outcome == ScheduledExecutionOutcome.POSTED

    # Inspect state in clean session
    with Session(migrated_engine) as session:
        updated = session.get(ScheduledTransfer, inst_id)
        assert updated is not None
        assert updated.last_run_date == today
        assert updated.next_run_date == date(2026, 11, 2)  # DAILY +1
        assert updated.last_transaction_id is not None
        assert updated.failure_count == 0
        assert updated.last_failure_reason is None

        # Verify idempotency row
        key_str = f"sched-{inst_id}-{today.isoformat()}"
        idem = session.execute(
            select(IdempotencyKey).where(IdempotencyKey.key == key_str)
        ).scalar_one_or_none()
        assert idem is not None

        # Verify transaction posted
        tx = session.get(Transaction, updated.last_transaction_id)
        assert tx is not None
        assert tx.status == TransactionStatus.POSTED
        assert tx.channel == "SCHEDULED"

        # Verify notification enqueued
        events = (
            session.execute(select(OutboxEvent).where(OutboxEvent.event_type == "TRANSFER_POSTED"))
            .scalars()
            .all()
        )
        assert len(events) == 1


def test_deterministic_idempotency_prevents_duplicate_post(
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, bob, payee = pair
    today = date(2026, 11, 1)

    with Session(migrated_engine) as session:
        inst = _create_instruction(session, alice, payee, next_run_date=today)
        inst_id = inst.id

    # First execution succeeds
    with Session(migrated_engine) as session:
        outcome1 = execute_scheduled_instruction(session, inst_id, today_utc=today)
        assert outcome1 == ScheduledExecutionOutcome.POSTED

    # Reset next_run_date to today to simulate a duplicate delivery
    with Session(migrated_engine) as session:
        row = session.get(ScheduledTransfer, inst_id)
        assert row is not None
        row.next_run_date = today
        session.commit()

    # Second execution triggers idempotency replay
    with Session(migrated_engine) as session:
        outcome2 = execute_scheduled_instruction(session, inst_id, today_utc=today)
        assert outcome2 == ScheduledExecutionOutcome.REPLAY

    # Verify balances only debited once
    with Session(migrated_engine) as session:
        src = session.get(Account, alice.account_id)
        dst = session.get(Account, bob.account_id)
        assert src is not None and dst is not None
        assert src.ledger_balance == 100_000 - AMOUNT
        assert dst.ledger_balance == AMOUNT


def test_advisory_lock_contention_skips_concurrent_worker(
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _, payee = pair
    today = date(2026, 11, 1)

    with Session(migrated_engine) as session:
        inst = _create_instruction(session, alice, payee, next_run_date=today)
        inst_id = inst.id

    session1 = Session(migrated_engine)
    session2 = Session(migrated_engine)
    try:
        # Worker 1 holds the advisory try-lock
        assert try_lock_scheduled_instruction(session1, inst_id) is True

        # Worker 2 attempts execution and immediately skips
        outcome = execute_scheduled_instruction(session2, inst_id, today_utc=today)
        assert outcome == ScheduledExecutionOutcome.SKIPPED_LOCKED
    finally:
        session1.rollback()
        session1.close()
        session2.close()


def test_insufficient_funds_retryable_failure_and_same_day_retry(
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, bob, payee = pair
    today = date(2026, 11, 1)

    # Create instruction for more funds than Alice has
    with Session(migrated_engine) as session:
        inst = _create_instruction(session, alice, payee, amount=200_000, next_run_date=today)
        inst_id = inst.id

    # Run execution -> Insufficient funds
    with Session(migrated_engine) as session:
        outcome = execute_scheduled_instruction(session, inst_id, today_utc=today)
        assert outcome == ScheduledExecutionOutcome.RETRYABLE_FAILURE

    with Session(migrated_engine) as session:
        inst_row = session.get(ScheduledTransfer, inst_id)
        assert inst_row is not None
        assert inst_row.failure_count == 1
        assert inst_row.last_failure_reason == "insufficient_funds"
        assert inst_row.last_failed_occurrence_date == today
        assert inst_row.next_run_date == today  # Same-day retry preserved

        # Outbox notification enqueued
        events = (
            session.execute(
                select(OutboxEvent).where(OutboxEvent.event_type == "SCHEDULED_TRANSFER_FAILED")
            )
            .scalars()
            .all()
        )
        assert len(events) == 1

        # Audit record created
        audit_rows = (
            session.execute(
                select(AuditLog).where(AuditLog.action == "SCHEDULED_TRANSFER_EXECUTION_FAILED")
            )
            .scalars()
            .all()
        )
        assert len(audit_rows) == 1

    # Second poll on same day with insufficient funds does NOT re-notify
    with Session(migrated_engine) as session:
        outcome = execute_scheduled_instruction(session, inst_id, today_utc=today)
        assert outcome == ScheduledExecutionOutcome.RETRYABLE_FAILURE

    with Session(migrated_engine) as session:
        events = (
            session.execute(
                select(OutboxEvent).where(OutboxEvent.event_type == "SCHEDULED_TRANSFER_FAILED")
            )
            .scalars()
            .all()
        )
        assert len(events) == 1  # De-duplicated!

    # Top-up Alice's account on the same day via official ledger deposit
    with Session(migrated_engine) as session:
        ledger.deposit(session, alice.account_id, 400_000)
        session.commit()

    # Retry succeeds on same day
    with Session(migrated_engine) as session:
        outcome = execute_scheduled_instruction(session, inst_id, today_utc=today)
        assert outcome == ScheduledExecutionOutcome.POSTED

    with Session(migrated_engine) as session:
        inst_row = session.get(ScheduledTransfer, inst_id)
        assert inst_row is not None
        assert inst_row.failure_count == 0
        assert inst_row.last_failure_reason is None
        assert inst_row.next_run_date == date(2026, 11, 2)


def test_next_day_unfunded_transfer_marked_missed(
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _, payee = pair
    yesterday = date(2026, 11, 1)
    today = date(2026, 11, 2)

    with Session(migrated_engine) as session:
        inst = _create_instruction(session, alice, payee, amount=200_000, next_run_date=yesterday)
        inst_id = inst.id

    # Yesterday: fails due to insufficient funds
    with Session(migrated_engine) as session:
        outcome = execute_scheduled_instruction(session, inst_id, today_utc=yesterday)
        assert outcome == ScheduledExecutionOutcome.RETRYABLE_FAILURE

    # Today: scheduler detects next_run_date (yesterday) < today
    with Session(migrated_engine) as session:
        outcome = execute_scheduled_instruction(session, inst_id, today_utc=today)
        assert outcome == ScheduledExecutionOutcome.RECOVERED

    with Session(migrated_engine) as session:
        inst_row = session.get(ScheduledTransfer, inst_id)
        assert inst_row is not None
        assert inst_row.next_run_date == today
        assert inst_row.failure_count == 0
        assert inst_row.last_failure_reason is None

        # Verify SCHEDULED_TRANSFER_MISSED audit record
        audit_rows = (
            session.execute(select(AuditLog).where(AuditLog.action == "SCHEDULED_TRANSFER_MISSED"))
            .scalars()
            .all()
        )
        assert len(audit_rows) == 1
        assert audit_rows[0].details["missed_date"] == yesterday.isoformat()

        # Verify SCHEDULED_TRANSFER_MISSED consolidated notification
        missed_events = (
            session.execute(
                select(OutboxEvent).where(OutboxEvent.event_type == "SCHEDULED_TRANSFER_MISSED")
            )
            .scalars()
            .all()
        )
        assert len(missed_events) == 1
        payload = missed_events[0].payload
        assert payload["missed_count"] == 1
        assert payload["first_missed_date"] == yesterday.isoformat()
        assert payload["last_missed_date"] == yesterday.isoformat()
        assert payload["next_eligible_date"] == today.isoformat()
        assert payload["completed"] is False


def test_multi_occurrence_outage_recovery_atomic(
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    """An outage occurs for 5 days. Recovery emits 5 audit records and 1 notification in 1 transaction."""
    alice, _, payee = pair
    missed_start = date(2026, 11, 1)
    today = date(2026, 11, 6)  # 5 days missed: Nov 1, 2, 3, 4, 5

    with Session(migrated_engine) as session:
        inst = _create_instruction(session, alice, payee, next_run_date=missed_start)
        inst_id = inst.id

    with Session(migrated_engine) as session:
        outcome = execute_scheduled_instruction(session, inst_id, today_utc=today)
        assert outcome == ScheduledExecutionOutcome.RECOVERED

    with Session(migrated_engine) as session:
        inst_row = session.get(ScheduledTransfer, inst_id)
        assert inst_row is not None
        assert inst_row.next_run_date == today

        # 5 distinct audit records
        audit_rows = (
            session.execute(
                select(AuditLog)
                .where(AuditLog.action == "SCHEDULED_TRANSFER_MISSED")
                .order_by(AuditLog.created_at.asc())
            )
            .scalars()
            .all()
        )
        assert len(audit_rows) == 5
        audited_dates = [r.details["missed_date"] for r in audit_rows]
        assert audited_dates == [
            "2026-11-01",
            "2026-11-02",
            "2026-11-03",
            "2026-11-04",
            "2026-11-05",
        ]

        # Exactly 1 consolidated outbox event
        events = (
            session.execute(
                select(OutboxEvent).where(OutboxEvent.event_type == "SCHEDULED_TRANSFER_MISSED")
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        p = events[0].payload
        assert p["missed_count"] == 5
        assert p["first_missed_date"] == "2026-11-01"
        assert p["last_missed_date"] == "2026-11-05"
        assert p["next_eligible_date"] == today.isoformat()
        assert p["completed"] is False


def test_recovery_rolls_back_atomically_on_error(
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _, payee = pair
    missed_start = date(2026, 11, 1)
    today = date(2026, 11, 4)

    with Session(migrated_engine) as session:
        inst = _create_instruction(session, alice, payee, next_run_date=missed_start)
        inst_id = inst.id

    # Simulate an unexpected database error during notification enqueue
    with (
        Session(migrated_engine) as session,
        patch(
            "pbip_api.modules.notifications.service.enqueue",
            side_effect=RuntimeError("disk full"),
        ),
        pytest.raises(RuntimeError, match="disk full"),
    ):
        execute_scheduled_instruction(session, inst_id, today_utc=today)

    # Verify no partial audit entries or outbox events persisted, state untouched
    with Session(migrated_engine) as session:
        inst_row = session.get(ScheduledTransfer, inst_id)
        assert inst_row is not None
        assert inst_row.next_run_date == missed_start  # Not advanced

        audits = (
            session.execute(select(AuditLog).where(AuditLog.action == "SCHEDULED_TRANSFER_MISSED"))
            .scalars()
            .all()
        )
        assert len(audits) == 0

        events = (
            session.execute(
                select(OutboxEvent).where(OutboxEvent.event_type == "SCHEDULED_TRANSFER_MISSED")
            )
            .scalars()
            .all()
        )
        assert len(events) == 0


def test_fraud_block_pauses_and_advances(
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
    settings: Settings,
) -> None:
    alice, _, payee = pair
    today = date(2026, 11, 1)

    with Session(migrated_engine) as session:
        inst = _create_instruction(session, alice, payee, next_run_date=today)
        inst_id = inst.id

    scorer = FixedDecisionScorer(FraudDecision.BLOCK)

    with Session(migrated_engine) as session:
        auth = AuthService(
            session,
            settings,
            get_password_service(settings),
            get_field_cipher(settings),
        )
        svc = PaymentsService(session, settings, scorer, auth)
        outcome = execute_scheduled_instruction(
            session, inst_id, today_utc=today, payments_service=svc
        )
        assert outcome == ScheduledExecutionOutcome.BLOCK

    with Session(migrated_engine) as session:
        inst_row = session.get(ScheduledTransfer, inst_id)
        assert inst_row is not None
        assert inst_row.status == ScheduledTransferStatus.PAUSED
        assert inst_row.pause_reason == SchedulePauseReason.FRAUD_BLOCK
        assert inst_row.next_run_date == date(2026, 11, 2)  # Advanced past blocked occurrence

        # Auto-pause audit
        audits = (
            session.execute(
                select(AuditLog).where(AuditLog.action == "SCHEDULED_TRANSFER_AUTO_PAUSED")
            )
            .scalars()
            .all()
        )
        assert len(audits) == 1
        assert audits[0].details["reason"] == "fraud_block"

        # Notification enqueued
        events = (
            session.execute(
                select(OutboxEvent).where(OutboxEvent.event_type == "SCHEDULED_TRANSFER_PAUSED")
            )
            .scalars()
            .all()
        )
        assert len(events) == 1


def test_fraud_step_up_converts_to_review_with_hold(
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
    settings: Settings,
) -> None:
    alice, _, payee = pair
    today = date(2026, 11, 1)

    with Session(migrated_engine) as session:
        inst = _create_instruction(session, alice, payee, next_run_date=today)
        inst_id = inst.id

    scorer = FixedDecisionScorer(FraudDecision.STEP_UP)

    with Session(migrated_engine) as session:
        auth = AuthService(
            session,
            settings,
            get_password_service(settings),
            get_field_cipher(settings),
        )
        svc = PaymentsService(session, settings, scorer, auth)
        outcome = execute_scheduled_instruction(
            session, inst_id, today_utc=today, payments_service=svc
        )
        assert outcome == ScheduledExecutionOutcome.REVIEW

    with Session(migrated_engine) as session:
        inst_row = session.get(ScheduledTransfer, inst_id)
        assert inst_row is not None
        assert inst_row.status == ScheduledTransferStatus.ACTIVE
        assert inst_row.next_run_date == date(2026, 11, 2)

        # Transaction held in review
        tx = session.get(Transaction, inst_row.last_transaction_id)
        assert tx is not None
        assert tx.status == TransactionStatus.PENDING_REVIEW

        # Funds held on source account
        src = session.get(Account, alice.account_id)
        assert src is not None
        assert src.held_amount == AMOUNT


def test_permanent_failure_frozen_account_pauses(
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _, payee = pair
    today = date(2026, 11, 1)

    with Session(migrated_engine) as session:
        inst = _create_instruction(session, alice, payee, next_run_date=today)
        inst_id = inst.id

        # Freeze Alice's account
        src = session.get(Account, alice.account_id)
        assert src is not None
        src.status = AccountStatus.FROZEN
        session.commit()

    with Session(migrated_engine) as session:
        outcome = execute_scheduled_instruction(session, inst_id, today_utc=today)
        assert outcome == ScheduledExecutionOutcome.PERMANENT_FAILURE

    with Session(migrated_engine) as session:
        inst_row = session.get(ScheduledTransfer, inst_id)
        assert inst_row is not None
        assert inst_row.status == ScheduledTransferStatus.PAUSED
        assert inst_row.pause_reason == SchedulePauseReason.CUSTOMER_ACTION_REQUIRED

        # Audit and notification created
        audits = (
            session.execute(
                select(AuditLog).where(AuditLog.action == "SCHEDULED_TRANSFER_AUTO_PAUSED")
            )
            .scalars()
            .all()
        )
        assert len(audits) == 1

        events = (
            session.execute(
                select(OutboxEvent).where(OutboxEvent.event_type == "SCHEDULED_TRANSFER_PAUSED")
            )
            .scalars()
            .all()
        )
        assert len(events) == 1


def test_customer_pause_concurrency_skips_processing(
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _, payee = pair
    today = date(2026, 11, 1)

    with Session(migrated_engine) as session:
        inst = _create_instruction(session, alice, payee, next_run_date=today)
        inst_id = inst.id

        # Customer pauses the instruction before worker acquires lock
        inst.status = ScheduledTransferStatus.PAUSED
        inst.pause_reason = SchedulePauseReason.CUSTOMER
        session.commit()

    with Session(migrated_engine) as session:
        outcome = execute_scheduled_instruction(session, inst_id, today_utc=today)
        assert outcome == ScheduledExecutionOutcome.SKIPPED_INACTIVE


def test_notification_rendering_scheduled_events(
    db: Session,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _, _ = pair
    user_id = alice.user_id

    # 1. SCHEDULED_TRANSFER_FAILED
    db.add(
        OutboxEvent(
            event_type="SCHEDULED_TRANSFER_FAILED",
            payload={
                "user_id": str(user_id),
                "account_id": str(alice.account_id),
                "amount_minor": 15_000,
                "currency": "INR",
                "failed_date": "2026-11-01",
                "reason": "insufficient_funds",
            },
        )
    )

    # 2. SCHEDULED_TRANSFER_PAUSED
    db.add(
        OutboxEvent(
            event_type="SCHEDULED_TRANSFER_PAUSED",
            payload={
                "user_id": str(user_id),
                "account_id": str(alice.account_id),
                "amount_minor": 15_000,
                "currency": "INR",
                "reason": "fraud_block",
            },
        )
    )

    # 3. SCHEDULED_TRANSFER_MISSED (single)
    db.add(
        OutboxEvent(
            event_type="SCHEDULED_TRANSFER_MISSED",
            payload={
                "user_id": str(user_id),
                "account_id": str(alice.account_id),
                "amount_minor": 15_000,
                "currency": "INR",
                "missed_count": 1,
                "first_missed_date": "2026-11-01",
                "last_missed_date": "2026-11-01",
                "next_eligible_date": "2026-11-02",
                "completed": False,
            },
        )
    )

    # 4. SCHEDULED_TRANSFER_MISSED (multiple)
    db.add(
        OutboxEvent(
            event_type="SCHEDULED_TRANSFER_MISSED",
            payload={
                "user_id": str(user_id),
                "account_id": str(alice.account_id),
                "amount_minor": 15_000,
                "currency": "INR",
                "missed_count": 3,
                "first_missed_date": "2026-11-01",
                "last_missed_date": "2026-11-03",
                "next_eligible_date": "2026-11-04",
                "completed": False,
            },
        )
    )
    db.commit()

    relayed = relay_outbox(db)
    assert relayed == 4

    notifs = (
        db.execute(
            select(Notification)
            .where(Notification.user_id == user_id)
            .order_by(Notification.created_at.asc())
        )
        .scalars()
        .all()
    )
    assert len(notifs) == 4

    # Check notification constraint compliance
    for n in notifs:
        assert n.resource_type is None
        assert n.account_id == alice.account_id

    # Check rendered titles and bodies
    assert notifs[0].title == "Scheduled transfer failed"
    assert "could not be completed" in notifs[0].body

    assert notifs[1].title == "Scheduled transfer paused"
    assert "was paused" in notifs[1].body

    assert notifs[2].title == "Scheduled transfer missed"
    assert "scheduled for 2026-11-01 was not executed" in notifs[2].body
    assert "Next scheduled transfer: 2026-11-02." in notifs[2].body

    assert notifs[3].title == "Scheduled transfers missed"
    assert "missed 3 occurrences between 2026-11-01 and 2026-11-03" in notifs[3].body
    assert "Next scheduled transfer: 2026-11-04." in notifs[3].body


def test_process_due_scheduled_transfers_orchestrator(
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, bob, payee = pair
    today = date(2026, 11, 1)

    with Session(migrated_engine) as session:
        # Create 2 active instructions due today
        _create_instruction(session, alice, payee, next_run_date=today)
        _create_instruction(session, alice, payee, next_run_date=today)

    results = process_due_scheduled_transfers(migrated_engine, today_utc=today)
    assert results[ScheduledExecutionOutcome.POSTED.value] == 2


# =============================================================================
# Review of #296: same rules as POST /api/v1/transfers, exactly-once under concurrency
# =============================================================================


def _service(session: Session, decision: FraudDecision, settings: Settings) -> PaymentsService:
    auth = AuthService(
        session, settings, get_password_service(settings), get_field_cipher(settings)
    )
    return PaymentsService(session, settings, FixedDecisionScorer(decision), auth)


def _transactions_from(session: Session, account_id: uuid.UUID) -> list[Transaction]:
    return list(
        session.execute(select(Transaction).where(Transaction.debit_account_id == account_id))
        .scalars()
        .all()
    )


def test_concurrent_workers_on_one_due_occurrence_post_it_exactly_once(
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    """Real threads, real PostgreSQL: duplicate Celery deliveries racing for one occurrence."""
    alice, bob, payee = pair
    today = date(2026, 11, 1)
    with Session(migrated_engine) as session:
        inst_id = _create_instruction(session, alice, payee, next_run_date=today).id

    workers = 6
    barrier = threading.Barrier(workers)

    def run(_: int) -> ScheduledExecutionOutcome:
        with Session(migrated_engine, expire_on_commit=False) as session:
            barrier.wait()
            return execute_scheduled_instruction(session, inst_id, today_utc=today)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(run, range(workers)))

    assert outcomes.count(ScheduledExecutionOutcome.POSTED) == 1, outcomes
    assert set(outcomes) <= {
        ScheduledExecutionOutcome.POSTED,
        ScheduledExecutionOutcome.SKIPPED_LOCKED,
        ScheduledExecutionOutcome.SKIPPED_INACTIVE,
    }
    with Session(migrated_engine) as session:
        assert len(_transactions_from(session, alice.account_id)) == 1
        src = session.get(Account, alice.account_id)
        dst = session.get(Account, bob.account_id)
        assert src is not None and dst is not None
        assert src.ledger_balance == 100_000 - AMOUNT
        assert dst.ledger_balance == AMOUNT
        row = session.get(ScheduledTransfer, inst_id)
        assert row is not None and row.next_run_date == date(2026, 11, 2)


def test_overlapping_beat_ticks_and_a_retried_task_never_post_twice(
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    """Two whole polling runs at once (a slow tick overlapping the next), then a redelivery."""
    alice, _, payee = pair
    today = date(2026, 11, 1)
    with Session(migrated_engine) as session:
        for _ in range(3):
            _create_instruction(session, alice, payee, next_run_date=today)

    barrier = threading.Barrier(2)

    def tick(_: int) -> dict[str, int]:
        barrier.wait()
        return process_due_scheduled_transfers(migrated_engine, today_utc=today)

    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = list(pool.map(tick, range(2)))
    retried = process_due_scheduled_transfers(migrated_engine, today_utc=today)

    assert sum(run[ScheduledExecutionOutcome.POSTED.value] for run in runs) == 3
    assert retried[ScheduledExecutionOutcome.POSTED.value] == 0
    with Session(migrated_engine) as session:
        assert len(_transactions_from(session, alice.account_id)) == 3
        src = session.get(Account, alice.account_id)
        assert src is not None and src.ledger_balance == 100_000 - 3 * AMOUNT


def test_a_scheduled_run_is_fraud_scored_and_its_feature_snapshot_persisted(
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
    settings: Settings,
) -> None:
    alice, _, payee = pair
    today = date(2026, 11, 1)
    with Session(migrated_engine) as session:
        inst_id = _create_instruction(session, alice, payee, next_run_date=today).id

    with Session(migrated_engine) as session:
        outcome = execute_scheduled_instruction(
            session,
            inst_id,
            today_utc=today,
            payments_service=_service(session, FraudDecision.REVIEW, settings),
        )
    assert outcome == ScheduledExecutionOutcome.REVIEW

    with Session(migrated_engine) as session:
        row = session.get(ScheduledTransfer, inst_id)
        assert row is not None and row.last_transaction_id is not None
        events = (
            session.execute(
                select(FraudEvent).where(FraudEvent.transaction_id == row.last_transaction_id)
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].decision == FraudDecision.REVIEW.value
        tx = session.get(Transaction, row.last_transaction_id)
        assert tx is not None and tx.status == TransactionStatus.PENDING_REVIEW
        held = session.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "TRANSFER_UNDER_REVIEW")
        ).scalars()
        assert len(list(held)) == 1  # the customer's bell says the run is held


def test_a_fraud_block_declines_the_run_and_tells_the_customer(
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
    settings: Settings,
) -> None:
    alice, bob, payee = pair
    today = date(2026, 11, 1)
    with Session(migrated_engine) as session:
        inst_id = _create_instruction(session, alice, payee, next_run_date=today).id

    with Session(migrated_engine) as session:
        outcome = execute_scheduled_instruction(
            session,
            inst_id,
            today_utc=today,
            payments_service=_service(session, FraudDecision.BLOCK, settings),
        )
    assert outcome == ScheduledExecutionOutcome.BLOCK

    with Session(migrated_engine) as session:
        row = session.get(ScheduledTransfer, inst_id)
        assert row is not None and row.last_transaction_id is not None
        tx = session.get(Transaction, row.last_transaction_id)
        assert tx is not None and tx.status == TransactionStatus.DECLINED
        dst = session.get(Account, bob.account_id)
        assert dst is not None and dst.ledger_balance == 0
        types = set(session.execute(select(OutboxEvent.event_type)).scalars())
        assert {"TRANSFER_DECLINED", "SCHEDULED_TRANSFER_PAUSED"} <= types


def test_a_fraud_block_on_the_final_run_completes_the_order_without_a_pause_reason(
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
    settings: Settings,
) -> None:
    """Regression (found while writing the LLD): a BLOCK on the last occurrence set COMPLETED
    but kept pause_reason=FRAUD_BLOCK, violating ck_scheduled_transfers_pause_reason_valid, so
    the commit failed and the run was retried for ever."""
    alice, bob, payee = pair
    today = date(2026, 11, 1)
    with Session(migrated_engine) as session:
        inst_id = _create_instruction(session, alice, payee, next_run_date=today, end_date=today).id

    with Session(migrated_engine) as session:
        outcome = execute_scheduled_instruction(
            session,
            inst_id,
            today_utc=today,
            payments_service=_service(session, FraudDecision.BLOCK, settings),
        )
    assert outcome == ScheduledExecutionOutcome.BLOCK

    with Session(migrated_engine) as session:
        row = session.get(ScheduledTransfer, inst_id)
        assert row is not None
        assert row.status == ScheduledTransferStatus.COMPLETED and row.pause_reason is None
        tx = session.get(Transaction, row.last_transaction_id)
        assert tx is not None and tx.status == TransactionStatus.DECLINED
        dst = session.get(Account, bob.account_id)
        assert dst is not None and dst.ledger_balance == 0


def test_the_daily_limit_applies_to_a_scheduled_run_and_it_retries_the_same_day(
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
    settings: Settings,
) -> None:
    alice, _, payee = pair
    today = date(2026, 11, 1)
    limited = settings.model_copy(update={"daily_transfer_limit_minor": AMOUNT - 1})
    with Session(migrated_engine) as session:
        inst_id = _create_instruction(session, alice, payee, next_run_date=today).id

    with Session(migrated_engine) as session:
        outcome = execute_scheduled_instruction(
            session,
            inst_id,
            today_utc=today,
            payments_service=_service(session, FraudDecision.ALLOW, limited),
        )
    assert outcome == ScheduledExecutionOutcome.RETRYABLE_FAILURE

    with Session(migrated_engine) as session:
        row = session.get(ScheduledTransfer, inst_id)
        assert row is not None
        assert row.status == ScheduledTransferStatus.ACTIVE
        assert row.next_run_date == today  # retried later today, never skipped silently
        assert row.last_failure_reason == "daily_limit_exceeded"
        assert _transactions_from(session, alice.account_id) == []
        failed = session.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "SCHEDULED_TRANSFER_FAILED")
        ).scalars()
        assert len(list(failed)) == 1


def test_the_worker_enforces_an_admins_runtime_daily_limit_without_a_restart(
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
    settings: Settings,
    make_staff: Callable[..., Actor],
) -> None:
    """TASK-108 / ADR-0031: the worker's service is built from the process settings, yet a limit
    an ADMIN lowered at runtime applies to its next run (no cache, read in the run's transaction)."""
    alice, _, payee = pair
    today = date(2026, 11, 1)
    admin = make_staff(RoleName.ADMIN)
    with Session(migrated_engine) as session:
        RuntimeConfigService(session, settings).change_setting(
            RuntimeSettingKey.DAILY_TRANSFER_LIMIT_MINOR,
            AMOUNT * 2,
            "Lowered for the scheduled-run test",
            admin.user_id,
            RequestMeta(),
        )
    with Session(migrated_engine) as session:
        inst_id = _create_instruction(
            session, alice, payee, amount=AMOUNT * 2 + 1, next_run_date=today
        ).id

    with Session(migrated_engine) as session:
        outcome = execute_scheduled_instruction(
            session,
            inst_id,
            today_utc=today,
            payments_service=_service(session, FraudDecision.ALLOW, settings),
        )
    assert outcome == ScheduledExecutionOutcome.RETRYABLE_FAILURE
    with Session(migrated_engine) as session:
        row = session.get(ScheduledTransfer, inst_id)
        assert row is not None and row.last_failure_reason == "daily_limit_exceeded"
        assert _transactions_from(session, alice.account_id) == []


def test_a_payee_that_is_no_longer_verified_is_not_paid_and_the_order_pauses(
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, bob, payee = pair
    today = date(2026, 11, 1)
    with Session(migrated_engine) as session:
        inst_id = _create_instruction(session, alice, payee, next_run_date=today).id
        stored = session.get(Payee, uuid.UUID(str(payee["id"])))
        assert stored is not None
        stored.status = PayeeStatus.PENDING_VERIFICATION  # e.g. edited, awaiting re-verification
        session.commit()

    with Session(migrated_engine) as session:
        outcome = execute_scheduled_instruction(session, inst_id, today_utc=today)
    assert outcome == ScheduledExecutionOutcome.PERMANENT_FAILURE

    with Session(migrated_engine) as session:
        row = session.get(ScheduledTransfer, inst_id)
        assert row is not None
        assert row.status == ScheduledTransferStatus.PAUSED
        assert row.pause_reason == SchedulePauseReason.CUSTOMER_ACTION_REQUIRED
        assert _transactions_from(session, alice.account_id) == []
        dst = session.get(Account, bob.account_id)
        assert dst is not None and dst.ledger_balance == 0
        paused = session.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "SCHEDULED_TRANSFER_PAUSED")
        ).scalars()
        assert len(list(paused)) == 1


def _failure_audits(session: Session, inst_id: uuid.UUID) -> list[AuditLog]:
    return list(
        session.execute(
            select(AuditLog)
            .where(
                AuditLog.action == "SCHEDULED_TRANSFER_EXECUTION_FAILED",
                AuditLog.resource_id == str(inst_id),
            )
            .order_by(AuditLog.created_at)
        )
        .scalars()
        .all()
    )


def test_identical_same_day_retries_write_one_audit_row_and_the_missed_row_counts_them(
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    """#305: a failing order retried every beat tick must not add an audit row per tick."""
    alice, _, payee = pair
    day = date(2026, 11, 1)
    with Session(migrated_engine) as session:
        inst_id = _create_instruction(session, alice, payee, amount=200_000, next_run_date=day).id

    for _ in range(5):
        with Session(migrated_engine) as session:
            outcome = execute_scheduled_instruction(session, inst_id, today_utc=day)
        assert outcome == ScheduledExecutionOutcome.RETRYABLE_FAILURE

    with Session(migrated_engine) as session:
        rows = _failure_audits(session, inst_id)
        assert len(rows) == 1
        assert rows[0].details["reason"] == "insufficient_funds"
        assert rows[0].details["failure_count"] == 1
        inst_row = session.get(ScheduledTransfer, inst_id)
        assert inst_row is not None
        assert inst_row.failure_count == 5  # every attempt is still counted on the order

    # The next day's recovery is the day's final outcome: it carries the attempts and the reason.
    with Session(migrated_engine) as session:
        outcome = execute_scheduled_instruction(session, inst_id, today_utc=date(2026, 11, 2))
    assert outcome == ScheduledExecutionOutcome.RECOVERED
    with Session(migrated_engine) as session:
        missed = (
            session.execute(
                select(AuditLog).where(
                    AuditLog.action == "SCHEDULED_TRANSFER_MISSED",
                    AuditLog.resource_id == str(inst_id),
                )
            )
            .scalars()
            .all()
        )
        assert len(missed) == 1
        assert missed[0].details["missed_date"] == day.isoformat()
        assert missed[0].details["failed_attempts"] == 5
        assert missed[0].details["last_failure_reason"] == "insufficient_funds"


def test_a_change_of_failure_reason_on_the_same_day_is_audited_again(
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
    settings: Settings,
) -> None:
    alice, _, payee = pair
    day = date(2026, 11, 1)
    with Session(migrated_engine) as session:
        inst_id = _create_instruction(session, alice, payee, amount=200_000, next_run_date=day).id

    with Session(migrated_engine) as session:
        assert (
            execute_scheduled_instruction(session, inst_id, today_utc=day)
            == ScheduledExecutionOutcome.RETRYABLE_FAILURE
        )
    with Session(migrated_engine) as session:
        ledger.deposit(session, alice.account_id, 400_000)
        session.commit()
    limited = settings.model_copy(update={"daily_transfer_limit_minor": 200_000 - 1})
    for _ in range(3):
        with Session(migrated_engine) as session:
            outcome = execute_scheduled_instruction(
                session,
                inst_id,
                today_utc=day,
                payments_service=_service(session, FraudDecision.ALLOW, limited),
            )
        assert outcome == ScheduledExecutionOutcome.RETRYABLE_FAILURE

    with Session(migrated_engine) as session:
        reasons = [row.details["reason"] for row in _failure_audits(session, inst_id)]
        assert reasons == ["insufficient_funds", "daily_limit_exceeded"]
        failed = session.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "SCHEDULED_TRANSFER_FAILED")
        ).scalars()
        assert len(list(failed)) == 1  # the customer is still told once a day
