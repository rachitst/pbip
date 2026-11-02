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
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session, sessionmaker

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
    assert hold.resolved_at is not None  # TASK-086: when the reservation ended is recorded
    assert _balances(db, a, b) == [(1_000, 0), (0, 0)]  # released funds were not captured too


def test_capturing_a_hold_records_when_it_was_resolved(db: Session) -> None:
    """TASK-086 (surviving mutant: resolved_at=None on capture)."""
    a, b = make_account(db, 1_000), make_account(db)
    txn = _header(db, a, b, 250)
    hold = ledger.place_hold(db, txn)
    ledger.capture_hold_and_post(db, txn)
    db.commit()
    db.refresh(hold)
    assert hold.status == HoldStatus.CAPTURED and hold.resolved_at is not None
    assert _balances(db, a, b) == [(750, 0), (250, 0)]


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


@pytest.mark.parametrize("missing_leg", ["debit_account_id", "credit_account_id"])
def test_reversal_refuses_a_transfer_missing_either_leg(db: Session, missing_leg: str) -> None:
    """TASK-086 (surviving mutant: `or` -> `and`): a header that lost ONE leg (corrupted data) must not
    be reversed; reversing it would post a one-sided compensating entry. No DB constraint prevents
    the state, so this guard is the only defence."""
    a, b = make_account(db, 1_000), make_account(db)
    original = transfer(db, a, b, 300)
    db.commit()
    before = db.execute(select(func.count()).select_from(Transaction)).scalar_one()
    # Corrupt only the transaction header; balances and entries are never written outside the ledger.
    db.execute(update(Transaction).where(Transaction.id == original.id).values({missing_leg: None}))
    db.commit()
    with pytest.raises(
        ledger.LedgerInvariantError, match=r"^transaction has no debit/credit instruction$"
    ):
        ledger.reverse_transaction(db, original.id, None, "corrupted header")
    db.rollback()
    assert db.execute(select(func.count()).select_from(Transaction)).scalar_one() == before
    assert _balances(db, a, b) == [(700, 0), (300, 0)]


# --------------------------------------------------------------- TASK-086 mutation gaps


def test_an_account_with_no_entries_has_a_zero_entry_balance(db: Session) -> None:
    empty = make_account(db)
    assert ledger.entry_balance(db, empty.id) == 0


@pytest.mark.parametrize("missing_leg", ["debit_account_id", "credit_account_id"])
def test_capturing_a_hold_refuses_a_header_missing_either_leg(
    db: Session, missing_leg: str
) -> None:
    """Capture would post a one-sided transfer; the guard refuses and nothing moves."""
    a, b = make_account(db, 1_000), make_account(db)
    txn = _header(db, a, b, 200)
    ledger.place_hold(db, txn)
    db.commit()
    db.execute(update(Transaction).where(Transaction.id == txn.id).values({missing_leg: None}))
    db.commit()
    db.refresh(txn)
    with pytest.raises(
        ledger.LedgerInvariantError, match=r"^transaction has no debit/credit instruction$"
    ):
        ledger.capture_hold_and_post(db, txn)
    db.rollback()
    assert _balances(db, a, b) == [(1_000, 200), (0, 0)]  # still only held, never posted


def test_ledger_errors_carry_their_exact_messages(db: Session) -> None:
    a, b = make_account(db, 1_000), make_account(db)
    with pytest.raises(NotFoundError, match=r"^transaction not found$"):
        ledger.reverse_transaction(db, uuid.uuid4(), None, "missing")
    db.rollback()
    txn = _header(db, a, b, 100)
    with pytest.raises(ConflictError, match=r"^no active hold for this transaction$"):
        ledger.release_hold(db, txn)
    db.rollback()
    deposit = db.execute(
        select(Transaction).where(Transaction.type == TransactionType.DEPOSIT).limit(1)
    ).scalar_one()
    with pytest.raises(ConflictError, match=r"^only posted transfers can be reversed$"):
        ledger.reverse_transaction(db, deposit.id, None, "not a transfer")
    db.rollback()


def test_a_reversal_names_who_made_it_and_keeps_the_reference_within_140(db: Session) -> None:
    a, b = make_account(db, 1_000), make_account(db)
    actor = db.execute(select(User.id).limit(1)).scalar_one()
    original = transfer(db, a, b, 100)
    db.commit()
    reversal = ledger.reverse_transaction(db, original.id, actor, "x" * 300)
    db.commit()
    assert reversal.initiated_by_user_id == actor
    assert reversal.reference == ("Reversal: " + "x" * 300)[:140] and len(reversal.reference) == 140


def test_the_ledger_posts_a_single_minor_unit(db: Session) -> None:
    """The smallest legal amount is 1 paisa; the positivity guard must not reject it."""
    a, b = make_account(db, 10), make_account(db)
    transfer(db, a, b, 1)
    db.commit()
    assert _balances(db, a, b) == [(9, 0), (1, 0)]


def test_a_deposit_is_labelled_and_channelled_by_default(db: Session) -> None:
    a = make_account(db)
    deposit = ledger.deposit(db, a.id, 5)
    db.commit()
    assert (deposit.reference, deposit.channel) == ("Deposit", "WEB")


def test_posting_guards_carry_their_exact_messages(db: Session) -> None:
    a, b = make_account(db, 1_000), make_account(db)
    debit, credit = EntryDirection.DEBIT, EntryDirection.CREDIT
    with pytest.raises(NotFoundError, match=r"^account not found$"):
        ledger.deposit(db, uuid.uuid4(), 100)
    db.rollback()
    txn = _header(db, a, b, 100)
    with pytest.raises(
        ledger.LedgerInvariantError, match=r"^a transaction needs at least one leg$"
    ):
        ledger.post_transaction(db, txn, [])
    with pytest.raises(ledger.LedgerInvariantError, match=r"^leg amounts must be positive$"):
        ledger.post_transaction(db, txn, [ledger.Leg(a.id, debit, 0), ledger.Leg(b.id, credit, 0)])
    with pytest.raises(
        ledger.LedgerInvariantError, match=r"^unbalanced legs: debits=100 credits=99$"
    ):
        ledger.post_transaction(
            db, txn, [ledger.Leg(a.id, debit, 100), ledger.Leg(b.id, credit, 99)]
        )
    with pytest.raises(ledger.InsufficientFundsError, match=r"^insufficient funds$"):
        ledger.post_transaction(
            db, txn, [ledger.Leg(a.id, debit, 5_000), ledger.Leg(b.id, credit, 5_000)]
        )
    db.rollback()
    foreign = _header(db, a, b, 100, currency="USD")
    with pytest.raises(UnprocessableError, match=r"^currency mismatch$"):
        ledger.post_transaction(db, foreign, ledger.transfer_legs(foreign))
    db.rollback()
    _freeze(db, b)
    db.flush()
    with pytest.raises(ledger.AccountNotActiveError, match=r"^account is not active$"):
        ledger.deposit(db, b.id, 1)
    db.rollback()


@pytest.mark.parametrize("missing", ["debit", "credit"])
def test_legs_and_holds_refuse_a_header_without_an_account(db: Session, missing: str) -> None:
    """Pure guards on an unsaved header: nothing is written, so no rollback is needed."""
    account_id = uuid.uuid4()
    header = Transaction(
        type=TransactionType.TRANSFER,
        status=TransactionStatus.INITIATED,
        amount=100,
        currency="INR",
        debit_account_id=None if missing == "debit" else account_id,
        credit_account_id=None if missing == "credit" else account_id,
    )
    with pytest.raises(
        ledger.LedgerInvariantError, match=r"^transaction has no debit/credit instruction$"
    ):
        ledger.transfer_legs(header)
    if missing == "debit":
        with pytest.raises(
            ledger.LedgerInvariantError, match=r"^transaction has no debit account$"
        ):
            ledger.place_hold(db, header)


def test_entry_listing_is_bounded_and_newest_first_by_default(db: Session) -> None:
    """The page query itself is LIMITed (the caller asks for limit + 1): an unbounded query would
    read an account's whole history on every page."""
    a, b = make_account(db, 1_000), make_account(db)
    for amount in (1, 2, 3):
        transfer(db, a, b, amount)
    db.commit()
    rows = ledger.list_account_entries(db, a.id, 2, None)
    assert [(entry.amount, txn.amount) for entry, txn in rows] == [(3, 3), (2, 2)]


def test_locking_reads_the_committed_balance_not_the_one_already_in_the_session(
    db: Session, migrated_engine: Engine
) -> None:
    """A request loads the source account (ownership check) BEFORE taking the row lock. If another
    request spends from it in between, the lock must refresh the row: posting against the stale
    balance would overdraw the account and overwrite the other request's debit (lost update)."""
    a, b = make_account(db, 1_000), make_account(db)
    assert db.get_one(Account, a.id).ledger_balance == 1_000  # now cached in this session
    with sessionmaker(bind=migrated_engine)() as other:
        transfer(other, other.get_one(Account, a.id), other.get_one(Account, b.id), 600)
        other.commit()
    with pytest.raises(ledger.InsufficientFundsError, match=r"^insufficient funds$"):
        transfer(db, a, b, 600)
    db.rollback()
    with sessionmaker(bind=migrated_engine)() as fresh:
        assert fresh.get_one(Account, a.id).ledger_balance == 400
        assert fresh.get_one(Account, b.id).ledger_balance == 600
