"""Ledger engine guard paths: every refusal raises the right error AND leaves no partial state.

AGENTS.md section 4: business failures must leave NO trace; programming errors that would break
double entry raise LedgerInvariantError before anything is written.
"""

import uuid

import pytest
from pbip_api.core.errors import ConflictError, NotFoundError, UnprocessableError
from pbip_api.modules.accounts.models import Account, AccountStatus, AccountType
from pbip_api.modules.accounts.service import open_account
from pbip_api.modules.auth.models import User
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import (
    EntryDirection,
    Hold,
    HoldStatus,
    Transaction,
    TransactionEntry,
    TransactionStatus,
    TransactionType,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


def make_account(db: Session, balance: int = 0) -> Account:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:10]}@guards.test", password_hash="x")
    db.add(user)
    db.flush()
    customer = Customer(id=uuid.uuid4(), user_id=user.id, full_name="Guard Test")
    db.add(customer)
    db.flush()
    account = open_account(db, customer.id, AccountType.SAVINGS)
    if balance:
        ledger.deposit(db, account.id, balance)
    db.commit()
    return account


def transfer(db: Session, source: Account, target: Account, amount: int) -> Transaction:
    txn = _header(db, source, target, amount)
    ledger.post_transaction(db, txn, ledger.transfer_legs(txn))
    return txn


def _counts(db: Session) -> tuple[int, int, int]:
    return (
        db.execute(select(func.count()).select_from(Transaction)).scalar_one(),
        db.execute(select(func.count()).select_from(TransactionEntry)).scalar_one(),
        db.execute(select(func.count()).select_from(Hold)).scalar_one(),
    )


def _balances(db: Session, *accounts: Account) -> list[tuple[int, int]]:
    db.expire_all()
    rows = [db.get_one(Account, a.id) for a in accounts]
    return [(a.ledger_balance, a.held_amount) for a in rows]


def _header(
    db: Session, source: Account, target: Account, amount: int, currency: str = "INR"
) -> Transaction:
    return ledger.create_transaction(
        db,
        type_=TransactionType.TRANSFER,
        amount=amount,
        currency=currency,
        debit_account_id=source.id,
        credit_account_id=target.id,
    )


def _freeze(db: Session, account: Account) -> None:
    db.get_one(Account, account.id).status = AccountStatus.FROZEN
    db.commit()


# ---------------------------------------------------------------- account checks


def test_unknown_account_is_not_found_and_nothing_is_written(db: Session) -> None:
    before = _counts(db)
    with pytest.raises(NotFoundError):
        ledger.deposit(db, uuid.uuid4(), 1_000)
    db.rollback()
    assert _counts(db) == before


def test_frozen_accounts_can_neither_receive_nor_send(db: Session) -> None:
    funded, frozen = make_account(db, 2_000), make_account(db, 500)
    _freeze(db, frozen)
    before, balances = _counts(db), _balances(db, funded, frozen)

    with pytest.raises(ledger.AccountNotActiveError):
        ledger.deposit(db, frozen.id, 100)
    db.rollback()
    with pytest.raises(ledger.AccountNotActiveError):
        transfer(db, frozen, funded, 100)
    db.rollback()
    with pytest.raises(ledger.AccountNotActiveError):
        ledger.place_hold(db, _header(db, frozen, funded, 100))
    db.rollback()

    assert _counts(db) == before
    assert _balances(db, funded, frozen) == balances


def test_currency_mismatch_is_refused_before_any_entry(db: Session) -> None:
    a, b = make_account(db, 1_000), make_account(db)
    before = _counts(db)
    txn = _header(db, a, b, 100, currency="USD")
    with pytest.raises(UnprocessableError, match="currency mismatch"):
        ledger.post_transaction(db, txn, ledger.transfer_legs(txn))
    db.rollback()
    assert _counts(db) == before
    assert _balances(db, a, b) == [(1_000, 0), (0, 0)]


# ------------------------------------------------------- double-entry invariants


@pytest.mark.parametrize(
    ("legs_factory", "message"),
    [
        (lambda a, b: [], "at least one leg"),
        (
            lambda a, b: [
                ledger.Leg(a, EntryDirection.DEBIT, 0),
                ledger.Leg(b, EntryDirection.CREDIT, 0),
            ],
            "must be positive",
        ),
    ],
    ids=["no-legs", "zero-amount-legs"],
)
def test_malformed_legs_are_programming_errors(
    db: Session, legs_factory: object, message: str
) -> None:
    a, b = make_account(db, 1_000), make_account(db)
    txn = _header(db, a, b, 100)
    with pytest.raises(ledger.LedgerInvariantError, match=message):
        ledger.post_transaction(db, txn, legs_factory(a.id, b.id))  # type: ignore[operator]  # parametrized factory
    db.rollback()
    assert _balances(db, a, b) == [(1_000, 0), (0, 0)]


def test_headers_without_an_instruction_cannot_be_posted_held_or_captured(db: Session) -> None:
    incomplete = Transaction(
        type=TransactionType.TRANSFER,
        status=TransactionStatus.INITIATED,
        amount=100,
        currency="INR",
    )
    with pytest.raises(ledger.LedgerInvariantError):
        ledger.transfer_legs(incomplete)
    with pytest.raises(ledger.LedgerInvariantError, match="no debit account"):
        ledger.place_hold(db, incomplete)
    with pytest.raises(ledger.LedgerInvariantError):
        ledger.capture_hold_and_post(db, incomplete)


# ------------------------------------------------------------------------ holds


def test_a_hold_larger_than_the_spendable_balance_is_refused(db: Session) -> None:
    a, b = make_account(db, 300), make_account(db)
    before = _counts(db)
    with pytest.raises(ledger.InsufficientFundsError):
        ledger.place_hold(db, _header(db, a, b, 301))
    db.rollback()
    assert _counts(db) == before
    assert _balances(db, a) == [(300, 0)]


def test_a_hold_can_be_resolved_only_once(db: Session) -> None:
    a, b = make_account(db, 1_000), make_account(db)
    txn = _header(db, a, b, 250)
    hold = ledger.place_hold(db, txn)
    ledger.release_hold(db, txn)
    db.commit()

    for resolve in (ledger.release_hold, ledger.capture_hold_and_post):
        with pytest.raises(ConflictError, match="no active hold"):
            resolve(db, txn)
        db.rollback()

    db.refresh(hold)
    assert hold.status == HoldStatus.RELEASED
    assert _balances(db, a, b) == [(1_000, 0), (0, 0)]  # released funds were not captured too


def test_resolving_a_transaction_that_never_had_a_hold_is_a_conflict(db: Session) -> None:
    a, b = make_account(db, 1_000), make_account(db)
    txn = _header(db, a, b, 100)
    with pytest.raises(ConflictError):
        ledger.release_hold(db, txn)
    db.rollback()
    assert _balances(db, a, b) == [(1_000, 0), (0, 0)]


# -------------------------------------------------------------------- reversals


def test_reversal_guards(db: Session) -> None:
    a, b = make_account(db, 1_000), make_account(db)
    with pytest.raises(NotFoundError):
        ledger.reverse_transaction(db, uuid.uuid4(), None, "no such transaction")
    db.rollback()

    deposit = db.execute(
        select(Transaction).where(Transaction.type == TransactionType.DEPOSIT).limit(1)
    ).scalar_one()
    with pytest.raises(ConflictError, match="only posted transfers"):
        ledger.reverse_transaction(db, deposit.id, None, "deposits are not reversible here")
    db.rollback()

    original = transfer(db, a, b, 400)
    db.commit()
    ledger.reverse_transaction(db, original.id, None, "customer dispute")
    db.commit()
    with pytest.raises(ConflictError):  # a reversed transfer cannot be reversed again
        ledger.reverse_transaction(db, original.id, None, "again")
    db.rollback()
    assert _balances(db, a, b) == [(1_000, 0), (0, 0)]
    assert ledger.entry_balance(db, a.id) == 1_000 and ledger.entry_balance(db, b.id) == 0
