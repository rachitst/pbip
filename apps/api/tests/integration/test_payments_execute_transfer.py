"""The reusable transfer core (TASK-015 / ADR-0021): ``_execute_transfer`` never commits.

A scheduled run must update its own state and the transfer in ONE commit, so the use case is split
from the interactive wrapper. These tests drive ``PaymentsService`` directly against PostgreSQL.
"""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from pbip_api.core.config import Settings
from pbip_api.core.metrics import TRANSFERS_TOTAL
from pbip_api.modules.accounts.models import Account
from pbip_api.modules.auth.service import AuthService, RequestMeta
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.ledger.models import (
    Hold,
    IdempotencyKey,
    Transaction,
    TransactionEntry,
    TransactionStatus,
    TransactionType,
)
from pbip_api.modules.ledger.service import InsufficientFundsError
from pbip_api.modules.notifications.models import OutboxEvent
from pbip_api.modules.payments.service import (
    PaymentsService,
    TransferChannel,
    TransferCommand,
    TransferExecution,
)
from pbip_ml.fraud.contract import FraudDecision
from pbip_ml.fraud.testing import FixedDecisionScorer
from pbip_test_support import Actor, FakeClock, age_payee, totp_code
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration

AMOUNT = 10_000


class CommitCounter:
    """Counts ``commit``/``rollback`` calls on one session without changing their behaviour."""

    def __init__(self, session: Session) -> None:
        self.commits = 0
        self.rollbacks = 0
        real_commit, real_rollback = session.commit, session.rollback

        def commit() -> None:
            self.commits += 1
            real_commit()

        def rollback() -> None:
            self.rollbacks += 1
            real_rollback()

        session.commit = commit  # type: ignore[method-assign]  # test spy on a session instance
        session.rollback = rollback  # type: ignore[method-assign]  # test spy on a session instance


@pytest.fixture
def pair(
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> tuple[Actor, Actor, dict[str, Any]]:
    alice, bob = make_customer(funds=1_000_000), make_customer()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)  # baseline rules stay quiet
    return alice, bob, payee


def build_service(
    db: Session,
    settings: Settings,
    auth_service_factory: Callable[[Session], AuthService],
    decision: FraudDecision,
) -> PaymentsService:
    return PaymentsService(db, settings, FixedDecisionScorer(decision), auth_service_factory(db))


def command_for(alice: Actor, payee: dict[str, Any], amount: int = AMOUNT) -> TransferCommand:
    return TransferCommand(
        from_account_id=alice.account_id,
        amount_minor=amount,
        currency="INR",
        payee_id=uuid.UUID(payee["id"]),
        to_account_id=None,
        reference="rent",
    )


def execute(
    service: PaymentsService,
    db: Session,
    alice: Actor,
    payee: dict[str, Any],
    *,
    key: str | None = None,
    amount: int = AMOUNT,
    channel: TransferChannel = TransferChannel.WEB,
    interactive: bool = True,
    meta: RequestMeta | None = None,
) -> TransferExecution:
    customer = db.get(Customer, alice.customer_id)
    assert customer is not None
    return service._execute_transfer(  # the unit under test
        user_id=alice.user_id,
        customer=customer,
        command=command_for(alice, payee, amount),
        idempotency_key=key or f"key-{uuid.uuid4().hex}",
        meta=meta,
        device_id=None,
        channel=channel,
        interactive=interactive,
    )


def count(engine: Engine, model: Any) -> int:
    """Committed rows only: a separate connection never sees another session's open transaction."""
    with sessionmaker(bind=engine)() as session:
        return int(session.execute(select(func.count()).select_from(model)).scalar_one())


def transfers(engine: Engine) -> int:
    """Committed TRANSFER transactions (the funding deposit is a separate DEPOSIT row)."""
    with sessionmaker(bind=engine)() as session:
        return int(
            session.execute(
                select(func.count())
                .select_from(Transaction)
                .where(Transaction.type == TransactionType.TRANSFER)
            ).scalar_one()
        )


def account_state(engine: Engine, account_id: uuid.UUID) -> tuple[int, int]:
    with sessionmaker(bind=engine)() as session:
        account = session.get(Account, account_id)
        assert account is not None
        return account.ledger_balance, account.held_amount


def enable_mfa(
    auth_service_factory: Callable[[Session], AuthService],
    engine: Engine,
    actor: Actor,
    clock: FakeClock,
) -> None:
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        service = auth_service_factory(session)
        secret = service.begin_mfa_enrollment(actor.user_id).secret
        service.confirm_mfa_enrollment(actor.user_id, totp_code(secret, clock), RequestMeta())


# ------------------------------------------------------------------ transaction boundary
def test_execute_transfer_never_commits_or_rolls_back_and_leaves_nothing_behind_on_rollback(
    db: Session,
    settings: Settings,
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _bob, payee = pair
    spy = CommitCounter(db)
    service = build_service(db, settings, auth_service_factory, FraudDecision.ALLOW)
    before = account_state(migrated_engine, alice.account_id)

    execution = execute(service, db, alice, payee)

    assert (spy.commits, spy.rollbacks) == (0, 0)
    assert execution.replayed is False
    assert execution.transaction.status == TransactionStatus.POSTED
    assert execution.outcome.status_code == 201
    assert execution.outcome.body["id"] == str(execution.transaction.id)
    # Nothing is visible to other connections until the CALLER commits.
    assert transfers(migrated_engine) == 0
    assert account_state(migrated_engine, alice.account_id) == before

    db.rollback()  # the caller's decision: abandon everything, including audit and outbox
    assert transfers(migrated_engine) == 0
    assert count(migrated_engine, IdempotencyKey) == 0
    assert account_state(migrated_engine, alice.account_id) == before


def test_caller_commit_makes_transfer_and_its_audit_and_outbox_durable_together(
    db: Session,
    settings: Settings,
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _bob, payee = pair
    service = build_service(db, settings, auth_service_factory, FraudDecision.ALLOW)
    audits = count(migrated_engine, AuditLog)
    outbox = count(migrated_engine, OutboxEvent)
    entries_before = count(migrated_engine, TransactionEntry)

    execution = execute(service, db, alice, payee)
    db.commit()

    assert transfers(migrated_engine) == 1
    assert count(migrated_engine, IdempotencyKey) == 1
    assert count(migrated_engine, TransactionEntry) == entries_before + 2  # one debit, one credit
    assert count(migrated_engine, AuditLog) == audits + 1
    assert count(migrated_engine, OutboxEvent) == outbox + 1
    assert account_state(migrated_engine, alice.account_id)[0] == 1_000_000 - AMOUNT
    assert execution.transaction.posted_at is not None


def test_public_create_transfer_commits_exactly_once_on_success(
    db: Session,
    settings: Settings,
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _bob, payee = pair
    spy = CommitCounter(db)
    service = build_service(db, settings, auth_service_factory, FraudDecision.ALLOW)
    customer = db.get(Customer, alice.customer_id)
    assert customer is not None

    result = service.create_transfer(
        user_id=alice.user_id,
        customer=customer,
        command=command_for(alice, payee),
        idempotency_key=f"key-{uuid.uuid4().hex}",
        meta=RequestMeta(ip="203.0.113.7", request_id="req-1"),
    )

    assert spy.commits == 1
    assert result.status_code == 201
    assert result.replayed is False
    with sessionmaker(bind=migrated_engine)() as session:
        stored = session.execute(
            select(Transaction).where(Transaction.type == TransactionType.TRANSFER)
        ).scalar_one()
        assert stored.channel == "WEB"  # interactive transfers stay on the WEB channel
        audit = session.execute(
            select(AuditLog).where(AuditLog.action == "TRANSFER_CREATED")
        ).scalar_one()
        assert audit.ip == "203.0.113.7"
        assert audit.request_id == "req-1"
        assert audit.details["channel"] == "WEB"


def test_failure_rolls_back_without_partial_ledger_state(
    db: Session,
    settings: Settings,
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _bob, payee = pair
    spy = CommitCounter(db)
    service = build_service(db, settings, auth_service_factory, FraudDecision.ALLOW)
    before = account_state(migrated_engine, alice.account_id)
    entries_before = count(migrated_engine, TransactionEntry)

    with pytest.raises(InsufficientFundsError):
        execute(service, db, alice, payee, amount=2_000_000)  # more than the 1,000,000 balance
    assert (spy.commits, spy.rollbacks) == (0, 0)  # the failure path is the caller's to roll back
    db.rollback()

    assert transfers(migrated_engine) == 0
    assert count(migrated_engine, TransactionEntry) == entries_before
    assert count(migrated_engine, IdempotencyKey) == 0
    assert account_state(migrated_engine, alice.account_id) == before


# ------------------------------------------------------------------------ idempotency
def test_replay_returns_the_original_transaction_and_writes_nothing(
    db: Session,
    settings: Settings,
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _bob, payee = pair
    service = build_service(db, settings, auth_service_factory, FraudDecision.ALLOW)
    key = f"sched-{uuid.uuid4().hex}"
    first = execute(service, db, alice, payee, key=key, channel=TransferChannel.SCHEDULED)
    db.commit()
    entries = count(migrated_engine, TransactionEntry)
    audits = count(migrated_engine, AuditLog)
    outbox = count(migrated_engine, OutboxEvent)
    balance = account_state(migrated_engine, alice.account_id)

    spy = CommitCounter(db)
    second = execute(service, db, alice, payee, key=key, channel=TransferChannel.SCHEDULED)

    assert second.replayed is True
    assert second.transaction.id == first.transaction.id
    assert second.outcome.body == first.outcome.body
    assert second.outcome.replayed is True
    assert (spy.commits, spy.rollbacks) == (0, 0)
    db.rollback()
    assert transfers(migrated_engine) == 1
    assert count(migrated_engine, TransactionEntry) == entries
    assert count(migrated_engine, AuditLog) == audits
    assert count(migrated_engine, OutboxEvent) == outbox
    assert account_state(migrated_engine, alice.account_id) == balance


def test_public_replay_does_not_commit_or_count_again(
    db: Session,
    settings: Settings,
    auth_service_factory: Callable[[Session], AuthService],
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _bob, payee = pair
    service = build_service(db, settings, auth_service_factory, FraudDecision.ALLOW)
    customer = db.get(Customer, alice.customer_id)
    assert customer is not None
    kwargs: dict[str, Any] = {
        "user_id": alice.user_id,
        "customer": customer,
        "command": command_for(alice, payee),
        "idempotency_key": f"key-{uuid.uuid4().hex}",
        "meta": RequestMeta(),
    }
    first = service.create_transfer(**kwargs)
    spy = CommitCounter(db)
    again = service.create_transfer(**kwargs)

    assert again.replayed is True
    assert again.body == first.body
    assert spy.commits == 0


# ------------------------------------------------------------------- fraud decisions
@pytest.mark.parametrize(
    ("decision", "status", "ledger_delta", "held"),
    [
        (FraudDecision.ALLOW, TransactionStatus.POSTED, -AMOUNT, 0),
        (FraudDecision.REVIEW, TransactionStatus.PENDING_REVIEW, 0, AMOUNT),
        (FraudDecision.BLOCK, TransactionStatus.DECLINED, 0, 0),
    ],
)
def test_fraud_decisions_are_applied_by_the_shared_core(
    decision: FraudDecision,
    status: TransactionStatus,
    ledger_delta: int,
    held: int,
    db: Session,
    settings: Settings,
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _bob, payee = pair
    service = build_service(db, settings, auth_service_factory, decision)

    execution = execute(service, db, alice, payee, interactive=False)
    db.commit()

    assert execution.transaction.status == status
    assert account_state(migrated_engine, alice.account_id) == (1_000_000 + ledger_delta, held)
    if decision == FraudDecision.BLOCK:
        assert execution.transaction.decline_reason == "FRAUD_BLOCK"
        assert execution.outcome.status_code == 403
        assert count(migrated_engine, Hold) == 0


def test_interactive_step_up_with_mfa_still_waits_for_the_code(
    db: Session,
    settings: Settings,
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    clock: FakeClock,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _bob, payee = pair
    enable_mfa(auth_service_factory, migrated_engine, alice, clock)
    service = build_service(db, settings, auth_service_factory, FraudDecision.STEP_UP)

    execution = execute(service, db, alice, payee, interactive=True)
    db.commit()

    assert execution.transaction.status == TransactionStatus.PENDING_STEP_UP
    assert execution.outcome.body["step_up_required"] is True
    assert account_state(migrated_engine, alice.account_id) == (1_000_000, 0)


def test_interactive_step_up_without_mfa_is_still_held_for_review(
    db: Session,
    settings: Settings,
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _bob, payee = pair
    service = build_service(db, settings, auth_service_factory, FraudDecision.STEP_UP)

    execution = execute(service, db, alice, payee, interactive=True)
    db.commit()

    assert execution.transaction.status == TransactionStatus.PENDING_REVIEW
    assert account_state(migrated_engine, alice.account_id) == (1_000_000, AMOUNT)


def test_non_interactive_step_up_becomes_review_and_holds_funds_even_with_mfa(
    db: Session,
    settings: Settings,
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    clock: FakeClock,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _bob, payee = pair
    enable_mfa(auth_service_factory, migrated_engine, alice, clock)
    service = build_service(db, settings, auth_service_factory, FraudDecision.STEP_UP)

    execution = execute(service, db, alice, payee, interactive=False)
    db.commit()

    assert execution.transaction.status == TransactionStatus.PENDING_REVIEW
    assert execution.outcome.body["step_up_required"] is False
    assert account_state(migrated_engine, alice.account_id) == (1_000_000, AMOUNT)
    assert count(migrated_engine, Hold) == 1


# ----------------------------------------------------------------- channel and metadata
def test_scheduled_channel_is_recorded_and_no_request_metadata_is_invented(
    db: Session,
    settings: Settings,
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _bob, payee = pair
    service = build_service(db, settings, auth_service_factory, FraudDecision.ALLOW)

    execution = execute(
        service, db, alice, payee, channel=TransferChannel.SCHEDULED, interactive=False, meta=None
    )
    db.commit()

    with sessionmaker(bind=migrated_engine)() as session:
        stored = session.get(Transaction, execution.transaction.id)
        assert stored is not None
        assert stored.channel == "SCHEDULED"
        audit = session.execute(
            select(AuditLog).where(AuditLog.resource_id == str(execution.transaction.id))
        ).scalar_one()
        assert audit.action == "TRANSFER_CREATED"
        assert audit.actor_user_id == alice.user_id
        assert audit.ip is None
        assert audit.request_id is None
        assert audit.details["channel"] == "SCHEDULED"


def test_record_committed_counts_a_fresh_execution_but_never_a_replay(
    db: Session,
    settings: Settings,
    auth_service_factory: Callable[[Session], AuthService],
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _bob, payee = pair
    service = build_service(db, settings, auth_service_factory, FraudDecision.ALLOW)
    key = f"key-{uuid.uuid4().hex}"
    child = TRANSFERS_TOTAL.labels(TransactionStatus.POSTED.value)
    start = child._value.get()  # prometheus has no public reader for one labelled child

    first = execute(service, db, alice, payee, key=key)
    db.commit()
    service.record_committed(first)
    assert child._value.get() == start + 1

    replay = execute(service, db, alice, payee, key=key)
    service.record_committed(replay)
    assert child._value.get() == start + 1
