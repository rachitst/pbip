"""Ledger invariants: double entry, immutability, balance safety and concurrency."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from pbip_api.modules.accounts.models import Account, AccountType
from pbip_api.modules.accounts.service import open_account
from pbip_api.modules.auth.models import User
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import (
    EntryDirection,
    HoldStatus,
    Transaction,
    TransactionEntry,
    TransactionStatus,
    TransactionType,
)
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration


def make_account(session: Session, balance: int = 0) -> Account:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:10]}@ledger.test", password_hash="x")
    session.add(user)
    session.flush()
    customer = Customer(id=uuid.uuid4(), user_id=user.id, full_name="Ledger Test")
    session.add(customer)
    session.flush()
    account = open_account(session, customer.id, AccountType.SAVINGS)
    if balance:
        ledger.deposit(session, account.id, balance)
    session.commit()
    return account


def transfer(session: Session, source: Account, target: Account, amount: int) -> Transaction:
    txn = ledger.create_transaction(
        session,
        type_=TransactionType.TRANSFER,
        amount=amount,
        currency="INR",
        debit_account_id=source.id,
        credit_account_id=target.id,
    )
    ledger.post_transaction(session, txn, ledger.transfer_legs(txn))
    return txn


def test_deposit_writes_two_balanced_entries_and_updates_balances(db: Session) -> None:
    account = make_account(db, 5_000)
    entries = (
        db.execute(select(TransactionEntry).where(TransactionEntry.account_id == account.id))
        .scalars()
        .all()
    )
    assert (
        len(entries) == 1
        and entries[0].direction == EntryDirection.CREDIT
        and entries[0].balance_after == 5_000
    )
    db.refresh(account)
    clearing = db.get(Account, ledger.SYSTEM_CLEARING_ID)
    assert (
        account.ledger_balance == 5_000
        and clearing is not None
        and clearing.ledger_balance == -5_000
    )


def test_unbalanced_legs_are_refused_by_the_engine(db: Session) -> None:
    a, b = make_account(db, 1_000), make_account(db)
    txn = ledger.create_transaction(
        db,
        type_=TransactionType.TRANSFER,
        amount=100,
        currency="INR",
        debit_account_id=a.id,
        credit_account_id=b.id,
    )
    legs = [
        ledger.Leg(a.id, EntryDirection.DEBIT, 100),
        ledger.Leg(b.id, EntryDirection.CREDIT, 90),
    ]
    with pytest.raises(ledger.LedgerInvariantError):
        ledger.post_transaction(db, txn, legs)


def test_database_itself_rejects_an_unbalanced_transaction_at_commit(db: Session) -> None:
    """Even if application code is wrong, PostgreSQL refuses to commit unbalanced entries."""
    a, b = make_account(db, 1_000), make_account(db)
    txn = ledger.create_transaction(
        db,
        type_=TransactionType.TRANSFER,
        amount=100,
        currency="INR",
        debit_account_id=a.id,
        credit_account_id=b.id,
    )
    db.add(
        TransactionEntry(
            transaction_id=txn.id,
            account_id=a.id,
            direction=EntryDirection.DEBIT,
            amount=100,
            currency="INR",
            balance_after=900,
        )
    )
    with pytest.raises((IntegrityError, DBAPIError), match="unbalanced"):
        db.commit()
    db.rollback()


def test_entries_are_immutable(db: Session, migrated_engine: Engine) -> None:
    account = make_account(db, 1_000)
    with migrated_engine.connect() as connection:
        with pytest.raises((IntegrityError, DBAPIError), match="append-only"):
            connection.execute(
                text("UPDATE transaction_entries SET amount = 1 WHERE account_id = :a"),
                {"a": account.id},
            )
        connection.rollback()
        with pytest.raises((IntegrityError, DBAPIError), match="append-only"):
            connection.execute(
                text("DELETE FROM transaction_entries WHERE account_id = :a"), {"a": account.id}
            )


def test_database_check_constraint_blocks_overdrawing_even_with_raw_sql(
    db: Session, migrated_engine: Engine
) -> None:
    account = make_account(db, 1_000)
    with migrated_engine.connect() as connection, pytest.raises(IntegrityError):
        connection.execute(
            text("UPDATE accounts SET ledger_balance = -5 WHERE id = :a"), {"a": account.id}
        )


def test_insufficient_funds_changes_nothing(db: Session) -> None:
    a, b = make_account(db, 100), make_account(db)
    with pytest.raises(ledger.InsufficientFundsError):
        transfer(db, a, b, 101)
    db.rollback()
    db.refresh(a)
    assert a.ledger_balance == 100


def test_hold_reserves_funds_then_capture_posts_or_release_returns(db: Session) -> None:
    a, b = make_account(db, 1_000), make_account(db)
    txn = ledger.create_transaction(
        db,
        type_=TransactionType.TRANSFER,
        amount=400,
        currency="INR",
        debit_account_id=a.id,
        credit_account_id=b.id,
    )
    hold = ledger.place_hold(db, txn)
    txn.status = TransactionStatus.PENDING_REVIEW
    db.commit()
    db.refresh(a)
    assert (a.ledger_balance, a.held_amount, a.available_balance) == (1_000, 400, 600)
    with pytest.raises(ledger.InsufficientFundsError):  # held money cannot be spent twice
        transfer(db, a, b, 700)
    db.rollback()

    ledger.capture_hold_and_post(db, txn)
    db.commit()
    db.refresh(a), db.refresh(b), db.refresh(hold)
    assert (a.ledger_balance, a.held_amount, b.ledger_balance, hold.status) == (
        600,
        0,
        400,
        HoldStatus.CAPTURED,
    )

    txn2 = ledger.create_transaction(
        db,
        type_=TransactionType.TRANSFER,
        amount=100,
        currency="INR",
        debit_account_id=a.id,
        credit_account_id=b.id,
    )
    ledger.place_hold(db, txn2)
    ledger.release_hold(db, txn2)
    db.commit()
    db.refresh(a)
    assert (a.ledger_balance, a.held_amount) == (600, 0)


def test_reversal_is_a_compensating_transaction_not_an_edit(db: Session) -> None:
    a, b = make_account(db, 1_000), make_account(db)
    original = transfer(db, a, b, 250)
    db.commit()
    reversal = ledger.reverse_transaction(db, original.id, None, "customer dispute")
    db.commit()
    db.refresh(a), db.refresh(b), db.refresh(original)
    assert (a.ledger_balance, b.ledger_balance) == (1_000, 0)
    assert original.status == TransactionStatus.REVERSED and reversal.reversal_of_id == original.id
    count = db.execute(
        select(func.count())
        .select_from(TransactionEntry)
        .where(TransactionEntry.transaction_id == original.id)
    ).scalar_one()
    assert count == 2  # original entries untouched
    with pytest.raises(Exception, match="posted"):
        ledger.reverse_transaction(db, original.id, None, "again")


def _run_concurrently(engine: Engine, jobs: list[tuple[uuid.UUID, uuid.UUID, int]]) -> list[bool]:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    barrier = threading.Barrier(len(jobs))

    def run(job: tuple[uuid.UUID, uuid.UUID, int]) -> bool:
        source_id, target_id, amount = job
        with factory() as session:
            barrier.wait(timeout=20)
            try:
                txn = ledger.create_transaction(
                    session,
                    type_=TransactionType.TRANSFER,
                    amount=amount,
                    currency="INR",
                    debit_account_id=source_id,
                    credit_account_id=target_id,
                )
                ledger.post_transaction(session, txn, ledger.transfer_legs(txn))
                session.commit()
                return True
            except ledger.InsufficientFundsError:
                session.rollback()
                return False

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        return list(pool.map(run, jobs))


def test_concurrent_spending_never_overdraws(db: Session, migrated_engine: Engine) -> None:
    source, target = make_account(db, 1_000), make_account(db)
    results = _run_concurrently(migrated_engine, [(source.id, target.id, 100)] * 14)
    assert sum(results) == 10  # exactly ten 100s fit into 1,000
    db.expire_all()
    assert (
        db.get(Account, source.id).ledger_balance,
        db.get(Account, target.id).ledger_balance,
    ) == (0, 1_000)  # type: ignore[union-attr]


def test_opposing_concurrent_transfers_do_not_deadlock_and_conserve_money(
    db: Session, migrated_engine: Engine
) -> None:
    a, b = make_account(db, 10_000), make_account(db, 10_000)
    jobs = [(a.id, b.id, 10), (b.id, a.id, 10)] * 6
    assert all(_run_concurrently(migrated_engine, jobs))
    db.expire_all()
    total = db.get(Account, a.id).ledger_balance + db.get(Account, b.id).ledger_balance  # type: ignore[union-attr]
    assert total == 20_000


def test_global_ledger_always_sums_to_zero(db: Session, migrated_engine: Engine) -> None:
    a, b = make_account(db, 5_000), make_account(db, 700)
    _run_concurrently(migrated_engine, [(a.id, b.id, 50)] * 8)
    db.expire_all()
    net = db.execute(
        text(
            "SELECT COALESCE(SUM(CASE WHEN direction='CREDIT' THEN amount ELSE -amount END), 0) FROM transaction_entries"
        )
    ).scalar_one()
    balances = db.execute(select(func.sum(Account.ledger_balance))).scalar_one()
    assert net == 0 and balances == 0
