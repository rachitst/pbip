"""Issue #332 / ADR-0029: automatic payout of fixed deposits at maturity (real PostgreSQL)."""

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.core.errors import DomainError
from pbip_api.modules.accounts.models import Account, AccountStatus
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import (
    EntryDirection,
    IdempotencyKey,
    Transaction,
    TransactionEntry,
    TransactionType,
)
from pbip_api.modules.notifications.models import Notification, OutboxEvent
from pbip_api.modules.notifications.service import relay_outbox
from pbip_api.modules.wealth import service as wealth_service
from pbip_api.modules.wealth.maturity_service import (
    PayoutOutcome,
    pay_out_matured_deposit,
    payout_key,
    process_matured_deposits,
)
from pbip_api.modules.wealth.models import FixedDeposit
from pbip_test_support import Actor, FakeClock
from sqlalchemy import Engine, case, func, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

FD_SHORT = "20000000-0000-4000-a000-000000000001"  # 6.50%, 6 or 9 months, penalty 1%
FUNDS = 2_00_000_00
AMOUNT = 1_00_000_00
INTEREST_6M = 3_276_41  # FD_SHORT, INR 1 lakh, 6 months (ADR-0025 maths, test_wealth.py)
DAY = 86_400
DEPOSITS = ledger.SYSTEM_FIXED_DEPOSITS_ID


def _book(client: TestClient, actor: Actor, amount: int = AMOUNT) -> uuid.UUID:
    resp = client.post(
        "/api/v1/wealth/fixed-deposits/book",
        headers={**actor.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "product_id": FD_SHORT,
            "amount_minor": amount,
            "tenure_months": 6,
            "account_id": str(actor.account_id),
        },
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


def _at(clock: FakeClock, days: int) -> datetime:
    return datetime.fromtimestamp(clock.now + days * DAY, tz=UTC)


def _run(engine: Engine, settings: Settings, now: datetime) -> dict[str, int]:
    counts = process_matured_deposits(engine, now=now, settings=settings)
    return {k: v for k, v in counts.items() if v}


def _deposit(session: Session, deposit_id: uuid.UUID) -> FixedDeposit:
    deposit = session.get(FixedDeposit, deposit_id, populate_existing=True)
    assert deposit is not None
    return deposit


def _balance(session: Session, account_id: uuid.UUID | None) -> int:
    account = session.get(Account, account_id, populate_existing=True)
    assert account is not None
    return account.ledger_balance


def _set_status(session: Session, account_id: uuid.UUID | None, status: AccountStatus) -> None:
    account = session.get(Account, account_id, populate_existing=True)
    assert account is not None
    account.status = status  # a status change, not a balance: allowed outside the ledger
    session.commit()


def _closures(session: Session) -> list[Transaction]:
    return list(
        session.execute(
            select(Transaction).where(Transaction.type == TransactionType.FD_CLOSURE)
        ).scalars()
    )


def _audits(session: Session, action: str) -> list[AuditLog]:
    return list(session.execute(select(AuditLog).where(AuditLog.action == action)).scalars())


def _outbox(session: Session, event_type: str) -> int:
    stmt = select(func.count()).select_from(OutboxEvent).where(OutboxEvent.event_type == event_type)
    return int(session.execute(stmt).scalar_one())


def _assert_ledger_consistent(session: Session) -> None:
    signed = case(
        (TransactionEntry.direction == EntryDirection.CREDIT, TransactionEntry.amount),
        else_=-TransactionEntry.amount,
    )
    unbalanced = session.execute(
        select(TransactionEntry.transaction_id)
        .group_by(TransactionEntry.transaction_id)
        .having(func.sum(signed) != 0)
    ).all()
    assert unbalanced == []
    for account in session.execute(
        select(Account).execution_options(populate_existing=True)
    ).scalars():
        assert ledger.entry_balance(session, account.id) == account.ledger_balance


def test_a_matured_deposit_is_paid_once_to_its_account_audited_and_notified(
    client: TestClient,
    make_customer: Callable[..., Actor],
    migrated_engine: Engine,
    settings: Settings,
    db: Session,
    clock: FakeClock,
) -> None:
    alice = make_customer(funds=FUNDS)
    deposit_id = _book(client, alice)
    deposits_before = _balance(db, DEPOSITS)

    assert _run(migrated_engine, settings, _at(clock, 100)) == {}  # not matured yet
    assert _closures(db) == []

    assert _run(migrated_engine, settings, _at(clock, 200)) == {"PAID": 1}
    deposit = _deposit(db, deposit_id)
    assert (deposit.status, deposit.closure_kind) == ("CLOSED", "MATURED")
    assert deposit.paid_interest == INTEREST_6M == deposit.maturity_interest
    assert deposit.closed_at == _at(clock, 200) and deposit.payout_held_at is None
    assert _balance(db, alice.account_id) == FUNDS + INTEREST_6M
    assert _balance(db, DEPOSITS) == deposits_before - AMOUNT
    principal = db.get(Transaction, deposit.closure_transaction_id)
    interest = db.get(Transaction, deposit.interest_transaction_id)
    assert principal is not None and interest is not None
    assert (principal.type, principal.amount, principal.channel) == (
        TransactionType.FD_CLOSURE,
        AMOUNT,
        "SCHEDULED",
    )
    assert (interest.type, interest.amount, interest.channel) == (
        TransactionType.FD_INTEREST,
        INTEREST_6M,
        "SCHEDULED",
    )
    (audit,) = _audits(db, "FIXED_DEPOSIT_MATURED_PAID_OUT")
    assert audit.actor_user_id is None and audit.resource_id == str(deposit_id)
    assert audit.details["actor"] == "scheduler"
    assert audit.details["interest_minor"] == INTEREST_6M
    key = db.execute(
        select(IdempotencyKey).where(IdempotencyKey.key == payout_key(deposit_id))
    ).scalar_one()
    assert key.user_id == alice.user_id and key.transaction_id == principal.id

    relay_outbox(db)
    db.commit()
    (bell,) = db.execute(
        select(Notification).where(Notification.type == "FIXED_DEPOSIT_MATURED")
    ).scalars()
    assert bell.user_id == alice.user_id and bell.title == "Fixed deposit matured"
    assert bell.resource_type == "transaction" and bell.resource_id == principal.id
    assert "3,276.41" in bell.body

    # A later run selects nothing; a duplicated delivery for the same deposit replays.
    assert _run(migrated_engine, settings, _at(clock, 201)) == {}
    with Session(migrated_engine, expire_on_commit=False) as session:
        again = pay_out_matured_deposit(session, settings, deposit_id, _at(clock, 201))
    assert again == PayoutOutcome.REPLAY
    assert len(_closures(db)) == 1
    _assert_ledger_consistent(db)


def test_deposits_closed_early_or_withdrawn_manually_are_untouched(
    client: TestClient,
    make_customer: Callable[..., Actor],
    migrated_engine: Engine,
    settings: Settings,
    db: Session,
    clock: FakeClock,
) -> None:
    alice = make_customer(funds=3 * AMOUNT)
    early, manual = _book(client, alice), _book(client, alice)
    clock.advance(30 * DAY)
    closed_early = client.post(
        f"/api/v1/wealth/fixed-deposits/{early}/close",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
    )
    assert closed_early.json()["closure_kind"] == "PREMATURE"
    clock.advance(170 * DAY)
    withdrawn = client.post(
        f"/api/v1/wealth/fixed-deposits/{manual}/close",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
    )
    assert withdrawn.json()["closure_kind"] == "MATURED"
    balance = _balance(db, alice.account_id)

    assert _run(migrated_engine, settings, _at(clock, 1)) == {}
    for deposit_id in (early, manual):  # even a direct call does nothing
        with Session(migrated_engine, expire_on_commit=False) as session:
            outcome = pay_out_matured_deposit(session, settings, deposit_id, _at(clock, 1))
        assert outcome == PayoutOutcome.SKIPPED
    assert len(_closures(db)) == 2 and _balance(db, alice.account_id) == balance
    assert _audits(db, "FIXED_DEPOSIT_MATURED_PAID_OUT") == []
    assert (
        db.execute(
            select(func.count())
            .select_from(IdempotencyKey)
            .where(IdempotencyKey.key.like("fd-maturity/%"))
        ).scalar_one()
        == 0
    )


@pytest.mark.parametrize("status", [AccountStatus.FROZEN, AccountStatus.CLOSED])
def test_an_inactive_account_holds_the_payout_notifies_once_and_pays_when_active(
    status: AccountStatus,
    client: TestClient,
    make_customer: Callable[..., Actor],
    migrated_engine: Engine,
    settings: Settings,
    db: Session,
    clock: FakeClock,
) -> None:
    """ADR-0029 section 4: hold and retry; the money stays on SYS-FIXED-DEPOSITS meanwhile."""
    alice = make_customer(funds=FUNDS)
    deposit_id = _book(client, alice)
    deposits_balance = _balance(db, DEPOSITS)
    _set_status(db, alice.account_id, status)

    assert _run(migrated_engine, settings, _at(clock, 200)) == {"HELD": 1}
    deposit = _deposit(db, deposit_id)
    assert deposit.status == "ACTIVE" and deposit.payout_held_at == _at(clock, 200)
    assert _closures(db) == [] and _balance(db, DEPOSITS) == deposits_balance
    assert _balance(db, alice.account_id) == FUNDS - AMOUNT
    assert (
        db.execute(
            select(IdempotencyKey).where(IdempotencyKey.key == payout_key(deposit_id))
        ).scalar_one_or_none()
        is None
    )  # the rolled-back payout left no key behind

    assert _run(migrated_engine, settings, _at(clock, 201)) == {"STILL_HELD": 1}
    (held_audit,) = _audits(db, "FIXED_DEPOSIT_PAYOUT_HELD")
    assert held_audit.details["reason"] == "account_not_active"
    assert _outbox(db, "FIXED_DEPOSIT_PAYOUT_HELD") == 1  # notified once, not every hour
    relay_outbox(db)
    db.commit()
    (bell,) = db.execute(
        select(Notification).where(Notification.type == "FIXED_DEPOSIT_PAYOUT_HELD")
    ).scalars()
    assert bell.title == "Fixed deposit payout on hold" and bell.resource_type is None
    assert bell.account_id == alice.account_id

    _set_status(db, alice.account_id, AccountStatus.ACTIVE)  # operations reactivate it
    assert _run(migrated_engine, settings, _at(clock, 202)) == {"PAID": 1}
    deposit = _deposit(db, deposit_id)
    assert deposit.status == "CLOSED" and deposit.payout_held_at is None
    assert _balance(db, alice.account_id) == FUNDS + INTEREST_6M
    _assert_ledger_consistent(db)


def test_a_failure_before_commit_leaves_nothing_and_the_next_run_pays(
    client: TestClient,
    make_customer: Callable[..., Actor],
    migrated_engine: Engine,
    settings: Settings,
    db: Session,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker crash mid-use-case (simulated at the audit write, after both postings)."""
    alice = make_customer(funds=FUNDS)
    deposit_id = _book(client, alice)
    original = AuditService.record

    def failing(self: AuditService, *args: Any, **kwargs: Any) -> AuditLog:
        if args[1] == "FIXED_DEPOSIT_MATURED_PAID_OUT":
            raise RuntimeError("worker lost")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(AuditService, "record", failing)
    assert _run(migrated_engine, settings, _at(clock, 200)) == {"ERROR": 1}
    assert _closures(db) == [] and _deposit(db, deposit_id).status == "ACTIVE"
    assert _balance(db, alice.account_id) == FUNDS - AMOUNT
    assert _outbox(db, "FIXED_DEPOSIT_MATURED") == 0

    monkeypatch.setattr(AuditService, "record", original)
    assert _run(migrated_engine, settings, _at(clock, 200)) == {"PAID": 1}
    assert len(_closures(db)) == 1 and _balance(db, alice.account_id) == FUNDS + INTEREST_6M


def test_concurrent_payout_runs_pay_each_deposit_exactly_once(
    client: TestClient,
    make_customer: Callable[..., Actor],
    migrated_engine: Engine,
    settings: Settings,
    db: Session,
    clock: FakeClock,
) -> None:
    """Real threads, real PostgreSQL: a re-delivered task overlapping the original, six times."""
    alice, bob = make_customer(funds=3 * AMOUNT), make_customer(funds=FUNDS)
    deposit_ids = [_book(client, alice), _book(client, alice), _book(client, bob)]
    workers, now = 6, _at(clock, 200)
    barrier = threading.Barrier(workers)

    def run(_: int) -> dict[str, int]:
        barrier.wait()
        return process_matured_deposits(migrated_engine, now=now, settings=settings)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(run, range(workers)))
    assert sum(r["PAID"] for r in results) == len(deposit_ids)
    assert sum(r["ERROR"] + r["HELD"] for r in results) == 0
    assert len(_closures(db)) == len(deposit_ids)
    assert len(_audits(db, "FIXED_DEPOSIT_MATURED_PAID_OUT")) == len(deposit_ids)
    assert _outbox(db, "FIXED_DEPOSIT_MATURED") == len(deposit_ids)
    assert _balance(db, alice.account_id) == 3 * AMOUNT + 2 * INTEREST_6M
    assert _balance(db, bob.account_id) == FUNDS + INTEREST_6M
    assert all(_deposit(db, d).status == "CLOSED" for d in deposit_ids)
    _assert_ledger_consistent(db)


def test_payout_racing_a_customer_closure_pays_exactly_once(
    client: TestClient,
    make_customer: Callable[..., Actor],
    migrated_engine: Engine,
    settings: Settings,
    db: Session,
    clock: FakeClock,
) -> None:
    """Real threads: three job deliveries and three customer withdrawals of one matured deposit."""
    alice = make_customer(funds=FUNDS)
    deposit_id = _book(client, alice)
    now = _at(clock, 200)
    assert alice.customer_id is not None
    customer_id = alice.customer_id
    workers = 6
    barrier = threading.Barrier(workers)

    def act(i: int) -> str:
        with Session(migrated_engine, expire_on_commit=False) as session:
            barrier.wait()
            if i % 2 == 0:
                return pay_out_matured_deposit(session, settings, deposit_id, now).value
            try:
                wealth_service.close_fixed_deposit(
                    session,
                    settings,
                    customer_id=customer_id,
                    user_id=alice.user_id,
                    deposit_id=deposit_id,
                    idempotency_key=uuid.uuid4().hex,
                    meta=RequestMeta(),
                    now=now,
                )
            except DomainError as exc:
                session.rollback()
                return exc.code
            return "closed"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(act, range(workers)))
    winners = [o for o in outcomes if o in {"PAID", "closed"}]
    assert len(winners) == 1, outcomes
    assert set(outcomes) - {"PAID", "closed"} <= {"REPLAY", "SKIPPED", "deposit_closed"}
    assert len(_closures(db)) == 1
    assert _balance(db, alice.account_id) == FUNDS + INTEREST_6M
    _assert_ledger_consistent(db)
