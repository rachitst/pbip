"""``ledger.deposit(posted_at=...)`` and ``ledger.seed_payment`` (ADR-0048): back-dated postings
for the demo seed's history. The header and both entries carry the date, the ledger invariants
still hold, and the ledger refuses a date that would falsify a running balance or that is used
outside local/test."""

import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pbip_api.core.db import utcnow
from pbip_api.modules.accounts.models import Account, AccountType
from pbip_api.modules.accounts.service import open_account
from pbip_api.modules.auth.models import User
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import (
    EntryDirection,
    Transaction,
    TransactionEntry,
    TransactionStatus,
    TransactionType,
)
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration


def _account(session: Session) -> Account:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:10]}@ledger.test", password_hash="x")
    session.add(user)
    session.flush()
    customer = Customer(id=uuid.uuid4(), user_id=user.id, full_name="Backdate Test")
    session.add(customer)
    session.flush()
    account = open_account(session, customer.id, AccountType.SAVINGS)
    session.commit()
    return account


def _entries(session: Session, txn_id: uuid.UUID) -> list[TransactionEntry]:
    stmt = select(TransactionEntry).where(TransactionEntry.transaction_id == txn_id)
    return list(session.scalars(stmt).all())


def _headers(session: Session, account_id: uuid.UUID) -> int:
    stmt = select(func.count()).where(Transaction.credit_account_id == account_id)
    return int(session.execute(stmt).scalar_one())


def test_a_backdated_deposit_dates_the_header_and_both_entries_and_stays_balanced(
    db: Session,
) -> None:
    account = _account(db)
    when = datetime(2026, 9, 1, 4, 30, tzinfo=UTC)

    txn = ledger.deposit(
        db, account.id, 12_500_000, reference="Salary September 2026", posted_at=when
    )
    db.commit()
    db.expire_all()

    header = db.get(Transaction, txn.id)
    assert header is not None and header.status == TransactionStatus.POSTED
    assert header.created_at == when and header.posted_at == when
    entries = _entries(db, txn.id)
    assert {e.created_at for e in entries} == {when}
    assert sorted(e.direction for e in entries) == [EntryDirection.CREDIT, EntryDirection.DEBIT]
    assert {e.amount for e in entries} == {12_500_000}
    refreshed = db.get(Account, account.id)
    assert refreshed is not None and refreshed.ledger_balance == 12_500_000
    assert ledger.entry_balance(db, account.id) == refreshed.ledger_balance


def test_a_backdated_entry_is_still_immutable(db: Session, migrated_engine: Engine) -> None:
    account = _account(db)
    ledger.deposit(db, account.id, 1_000, posted_at=utcnow() - timedelta(days=40))
    db.commit()
    with migrated_engine.connect() as connection:
        with pytest.raises((IntegrityError, DBAPIError), match="append-only"):
            connection.execute(
                text("UPDATE transaction_entries SET created_at = now() WHERE account_id = :a"),
                {"a": account.id},
            )
        connection.rollback()
        with pytest.raises((IntegrityError, DBAPIError), match="append-only"):
            connection.execute(
                text("DELETE FROM transaction_entries WHERE account_id = :a"), {"a": account.id}
            )


def test_history_in_time_order_is_accepted_and_keeps_running_balances_true(db: Session) -> None:
    account = _account(db)
    first = utcnow() - timedelta(days=60)
    ledger.deposit(db, account.id, 1_000, posted_at=first)
    ledger.deposit(db, account.id, 2_000, posted_at=first)  # the same instant is not "before"
    ledger.deposit(db, account.id, 3_000, posted_at=first + timedelta(days=30))
    ledger.deposit(db, account.id, 4_000)  # now
    db.commit()

    rows = db.execute(
        select(TransactionEntry.created_at, TransactionEntry.balance_after)
        .where(TransactionEntry.account_id == account.id)
        .order_by(TransactionEntry.created_at, TransactionEntry.seq)
    ).all()
    assert [int(r.balance_after) for r in rows] == [1_000, 3_000, 6_000, 10_000]


@pytest.mark.parametrize(
    ("label", "make_when"),
    [
        # Computed when the test runs, not at collection: a long suite would otherwise turn
        # "5 minutes from now" into the past before this case executes.
        ("future", lambda: utcnow() + timedelta(minutes=5)),
        ("naive", lambda: datetime(2026, 9, 1, 4, 30)),  # noqa: DTZ001 - the refused input
    ],
)
def test_a_future_or_naive_date_is_refused_and_writes_nothing(
    db: Session, label: str, make_when: Callable[[], datetime]
) -> None:
    when = make_when()
    account = _account(db)
    with pytest.raises(ledger.LedgerInvariantError):
        ledger.deposit(db, account.id, 1_000, posted_at=when)
    db.rollback()
    assert _headers(db, account.id) == 0, label


def test_a_date_before_the_latest_entry_is_refused_and_writes_nothing(db: Session) -> None:
    """Back-dating before an existing entry would leave every later ``balance_after`` wrong (the
    entries are immutable, so they could never be corrected): statements would show a false
    running balance."""
    account = _account(db)
    ledger.deposit(db, account.id, 1_000)  # now
    db.commit()

    with pytest.raises(ledger.LedgerInvariantError, match="latest entry"):
        ledger.deposit(db, account.id, 5_000, posted_at=utcnow() - timedelta(days=30))
    db.rollback()

    assert _headers(db, account.id) == 1
    refreshed = db.get(Account, account.id)
    assert refreshed is not None and refreshed.ledger_balance == 1_000


def test_backdating_is_refused_in_staging_and_production(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = _account(db)
    monkeypatch.setattr(ledger, "get_settings", lambda: SimpleNamespace(is_deployed=True))
    with pytest.raises(ledger.LedgerInvariantError, match="staging/production"):
        ledger.deposit(db, account.id, 1_000, posted_at=utcnow() - timedelta(days=30))
    db.rollback()
    assert _headers(db, account.id) == 0
    ledger.deposit(db, account.id, 1_000)  # an ordinary deposit is unaffected
    db.commit()
    assert _headers(db, account.id) == 1


def test_concurrent_backdated_deposits_neither_deadlock_nor_lose_money(
    db: Session, migrated_engine: Engine
) -> None:
    """Back-dating takes the same sorted account locks before the header as every posting."""
    account = _account(db)
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False)
    when = utcnow() - timedelta(days=10)

    def post(amount: int) -> None:
        with factory() as session:
            ledger.deposit(session, account.id, amount, posted_at=when)
            session.commit()

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(post, [100] * 6))

    db.expire_all()
    refreshed = db.get(Account, account.id)
    assert refreshed is not None and refreshed.ledger_balance == 600
    assert ledger.entry_balance(db, account.id) == 600
    balances = db.scalars(
        select(TransactionEntry.balance_after)
        .where(TransactionEntry.account_id == account.id)
        .order_by(TransactionEntry.seq)
    ).all()
    assert [int(b) for b in balances] == [100, 200, 300, 400, 500, 600]


def test_a_seed_payment_is_a_dated_balanced_transfer_to_the_clearing_account(db: Session) -> None:
    account = _account(db)
    funded = utcnow() - timedelta(days=30)
    ledger.deposit(db, account.id, 10_000, posted_at=funded)
    when = funded + timedelta(days=1)

    txn = ledger.seed_payment(
        db, account.id, 2_500, reference="BigBasket groceries", posted_at=when
    )
    db.commit()
    db.expire_all()

    header = db.get(Transaction, txn.id)
    assert header is not None and header.status == TransactionStatus.POSTED
    assert header.type == TransactionType.TRANSFER
    assert (header.debit_account_id, header.credit_account_id) == (
        account.id,
        ledger.SYSTEM_CLEARING_ID,
    )
    assert header.created_at == when and header.posted_at == when
    assert {e.created_at for e in _entries(db, txn.id)} == {when}
    refreshed = db.get(Account, account.id)
    assert refreshed is not None and refreshed.ledger_balance == 7_500
    assert ledger.entry_balance(db, account.id) == 7_500


def _debits(session: Session, account_id: uuid.UUID) -> int:
    stmt = select(func.count()).where(Transaction.debit_account_id == account_id)
    return int(session.execute(stmt).scalar_one())


def test_a_seed_payment_the_account_cannot_afford_is_refused_and_writes_nothing(
    db: Session,
) -> None:
    account = _account(db)
    ledger.deposit(db, account.id, 1_000, posted_at=utcnow() - timedelta(days=3))
    db.commit()
    with pytest.raises(ledger.InsufficientFundsError):
        ledger.seed_payment(
            db, account.id, 1_001, reference="Rent", posted_at=utcnow() - timedelta(days=1)
        )
    db.rollback()
    assert _debits(db, account.id) == 0


def test_a_seed_payment_has_the_same_date_guards_as_a_backdated_deposit(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = _account(db)
    ledger.deposit(db, account.id, 5_000)  # now
    db.commit()
    for when in (utcnow() - timedelta(days=1), utcnow() + timedelta(minutes=5)):
        with pytest.raises(ledger.LedgerInvariantError):
            ledger.seed_payment(db, account.id, 100, reference="Rent", posted_at=when)
        db.rollback()
    monkeypatch.setattr(ledger, "get_settings", lambda: SimpleNamespace(is_deployed=True))
    with pytest.raises(ledger.LedgerInvariantError, match="staging/production"):
        ledger.seed_payment(db, account.id, 100, reference="Rent", posted_at=utcnow())
    db.rollback()
    assert _debits(db, account.id) == 0
