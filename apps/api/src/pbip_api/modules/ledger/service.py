"""The double-entry ledger engine. The ONLY code allowed to change account balances.

Concepts implemented (see docs/PROJECT_BLUEPRINT.md section 7):
* every posted transaction has >= 2 entries and SUM(debits) == SUM(credits) (checked in Python
  AND by a deferred constraint trigger in PostgreSQL);
* entries are immutable (trigger); corrections are made by compensating REVERSAL transactions;
* ``accounts.ledger_balance`` is a materialised running balance, updated in the SAME database
  transaction as the entries, under ``SELECT ... FOR UPDATE`` row locks taken in a global order
  (sorted by id) so concurrent transfers cannot deadlock or overdraw;
* holds reserve funds for transactions awaiting review without touching the ledger balance.

Entries are recorded from the account's own perspective: DEBIT decreases, CREDIT increases.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from pbip_api.core.db import utcnow
from pbip_api.core.errors import ConflictError, NotFoundError, UnprocessableError
from pbip_api.modules.accounts.models import Account, AccountStatus, AccountType
from pbip_api.modules.ledger.models import (
    EntryDirection,
    Hold,
    HoldStatus,
    Transaction,
    TransactionEntry,
    TransactionStatus,
    TransactionType,
)

SYSTEM_CLEARING_ID = UUID("00000000-0000-0000-0000-0000000000c1")
SYSTEM_FEES_ID = UUID("00000000-0000-0000-0000-0000000000c2")


class InsufficientFundsError(UnprocessableError):
    code = "insufficient_funds"


class AccountNotActiveError(UnprocessableError):
    code = "account_not_active"


class LedgerInvariantError(Exception):
    """A programming error: the caller tried to post something that violates double entry."""


@dataclass(frozen=True, slots=True)
class Leg:
    account_id: UUID
    direction: EntryDirection
    amount: int


def lock_accounts(session: Session, account_ids: Iterable[UUID]) -> dict[UUID, Account]:
    """Lock accounts in sorted id order and return fresh rows (prevents deadlocks)."""
    ids = sorted(set(account_ids))
    rows = (
        session.execute(
            select(Account)
            .where(Account.id.in_(ids))
            .order_by(Account.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        .scalars()
        .all()
    )
    if len(rows) != len(ids):
        raise NotFoundError("account not found")
    return {account.id: account for account in rows}


def _require_active(account: Account) -> None:
    if account.account_type != AccountType.SYSTEM and account.status != AccountStatus.ACTIVE:
        raise AccountNotActiveError("account is not active")


def transfer_legs(transaction: Transaction) -> list[Leg]:
    if transaction.debit_account_id is None or transaction.credit_account_id is None:
        raise LedgerInvariantError("transaction has no debit/credit instruction")
    return [
        Leg(transaction.debit_account_id, EntryDirection.DEBIT, transaction.amount),
        Leg(transaction.credit_account_id, EntryDirection.CREDIT, transaction.amount),
    ]


def create_transaction(
    session: Session,
    *,
    type_: TransactionType,
    amount: int,
    currency: str,
    debit_account_id: UUID,
    credit_account_id: UUID,
    initiated_by_user_id: UUID | None = None,
    payee_id: UUID | None = None,
    reference: str | None = None,
    channel: str = "WEB",
    reversal_of_id: UUID | None = None,
) -> Transaction:
    # Lock BOTH accounts (sorted) before inserting the header. The header's foreign keys take
    # KEY SHARE locks on the accounts; acquiring FOR UPDATE afterwards would deadlock with a
    # concurrent transaction doing the same. Found by test_concurrent_spending_never_overdraws.
    lock_accounts(session, [debit_account_id, credit_account_id])
    transaction = Transaction(
        type=type_,
        status=TransactionStatus.INITIATED,
        amount=amount,
        currency=currency,
        initiated_by_user_id=initiated_by_user_id,
        debit_account_id=debit_account_id,
        credit_account_id=credit_account_id,
        payee_id=payee_id,
        reference=reference,
        channel=channel,
        reversal_of_id=reversal_of_id,
    )
    session.add(transaction)
    session.flush()
    return transaction


def place_hold(session: Session, transaction: Transaction) -> Hold:
    """Reserve the transaction amount on the debit account (REVIEW decisions)."""
    if transaction.debit_account_id is None:
        raise LedgerInvariantError("transaction has no debit account")
    account = lock_accounts(session, [transaction.debit_account_id])[transaction.debit_account_id]
    _require_active(account)
    if account.spendable < transaction.amount:
        raise InsufficientFundsError("insufficient funds")
    account.held_amount += transaction.amount
    hold = Hold(account_id=account.id, transaction_id=transaction.id, amount=transaction.amount)
    session.add(hold)
    session.flush()
    return hold


def _active_hold(session: Session, transaction: Transaction) -> Hold:
    hold = session.execute(
        select(Hold)
        .where(Hold.transaction_id == transaction.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if hold is None or hold.status != HoldStatus.ACTIVE:
        raise ConflictError("no active hold for this transaction")
    return hold


def release_hold(session: Session, transaction: Transaction) -> None:
    """Give the reserved funds back (analyst rejected the transfer)."""
    hold = _active_hold(session, transaction)
    account = lock_accounts(session, [hold.account_id])[hold.account_id]
    account.held_amount -= hold.amount
    hold.status = HoldStatus.RELEASED
    hold.resolved_at = utcnow()
    session.flush()


def capture_hold_and_post(session: Session, transaction: Transaction) -> None:
    """Analyst released the transfer: convert the hold into real ledger entries."""
    if transaction.debit_account_id is None or transaction.credit_account_id is None:
        raise LedgerInvariantError("transaction has no debit/credit instruction")
    # Lock every account up front in sorted order (consistent order => no deadlocks).
    accounts = lock_accounts(session, [transaction.debit_account_id, transaction.credit_account_id])
    hold = _active_hold(session, transaction)
    accounts[hold.account_id].held_amount -= hold.amount
    hold.status = HoldStatus.CAPTURED
    hold.resolved_at = utcnow()
    post_transaction(session, transaction, transfer_legs(transaction))


def post_transaction(session: Session, transaction: Transaction, legs: list[Leg]) -> None:
    """Write balanced, immutable entries and update running balances atomically."""
    if not legs:
        raise LedgerInvariantError("a transaction needs at least one leg")
    if any(leg.amount <= 0 for leg in legs):
        raise LedgerInvariantError("leg amounts must be positive")
    debits = sum(leg.amount for leg in legs if leg.direction == EntryDirection.DEBIT)
    credits = sum(leg.amount for leg in legs if leg.direction == EntryDirection.CREDIT)
    if debits != credits:
        raise LedgerInvariantError(f"unbalanced legs: debits={debits} credits={credits}")

    accounts = lock_accounts(session, [leg.account_id for leg in legs])
    for leg in legs:
        account = accounts[leg.account_id]
        if account.currency != transaction.currency:
            raise UnprocessableError("currency mismatch")
        _require_active(account)
        if leg.direction == EntryDirection.DEBIT:
            if account.account_type != AccountType.SYSTEM and account.spendable < leg.amount:
                raise InsufficientFundsError("insufficient funds")
            account.ledger_balance -= leg.amount
        else:
            account.ledger_balance += leg.amount
        session.add(
            TransactionEntry(
                transaction_id=transaction.id,
                account_id=account.id,
                direction=leg.direction,
                amount=leg.amount,
                currency=transaction.currency,
                balance_after=account.ledger_balance,
            )
        )
    transaction.status = TransactionStatus.POSTED
    transaction.posted_at = utcnow()
    session.flush()


def deposit(
    session: Session,
    account_id: UUID,
    amount: int,
    currency: str = "INR",
    reference: str = "Deposit",
) -> Transaction:
    """Fund an account from the internal clearing account (used by seeding and tests)."""
    transaction = create_transaction(
        session,
        type_=TransactionType.DEPOSIT,
        amount=amount,
        currency=currency,
        debit_account_id=SYSTEM_CLEARING_ID,
        credit_account_id=account_id,
        reference=reference,
    )
    post_transaction(session, transaction, transfer_legs(transaction))
    return transaction


def reverse_transaction(
    session: Session, original_id: UUID, actor_user_id: UUID | None, reason: str
) -> Transaction:
    """Undo a POSTED transfer with a compensating transaction (history is never edited)."""
    original = session.execute(
        select(Transaction)
        .where(Transaction.id == original_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if original is None:
        raise NotFoundError("transaction not found")
    if original.status != TransactionStatus.POSTED or original.type != TransactionType.TRANSFER:
        raise ConflictError("only posted transfers can be reversed")
    if original.debit_account_id is None or original.credit_account_id is None:
        raise LedgerInvariantError("transaction has no debit/credit instruction")
    reversal = create_transaction(
        session,
        type_=TransactionType.REVERSAL,
        amount=original.amount,
        currency=original.currency,
        debit_account_id=original.credit_account_id,
        credit_account_id=original.debit_account_id,
        initiated_by_user_id=actor_user_id,
        reference=f"Reversal: {reason}"[:140],
        reversal_of_id=original.id,
    )
    post_transaction(session, reversal, transfer_legs(reversal))
    original.status = TransactionStatus.REVERSED
    session.flush()
    return reversal


def list_account_entries(
    session: Session,
    account_id: UUID,
    limit: int,
    after_seq: int | None,
    newer: bool = False,
) -> list[tuple[TransactionEntry, Transaction]]:
    """Keyset page of an account's entries by posting order (``seq``).

    ``newer=False`` walks back in time (newest first, ``seq < after_seq``); ``newer=True`` walks
    forward (oldest first, ``seq > after_seq``). The caller authorizes the account first.
    """
    order = TransactionEntry.seq.asc() if newer else TransactionEntry.seq.desc()
    stmt = (
        select(TransactionEntry, Transaction)
        .join(Transaction, Transaction.id == TransactionEntry.transaction_id)
        .where(TransactionEntry.account_id == account_id)
        .order_by(order)
        .limit(limit)
    )
    if after_seq is not None:
        bound = TransactionEntry.seq > after_seq if newer else TransactionEntry.seq < after_seq
        stmt = stmt.where(bound)
    return [(entry, txn) for entry, txn in session.execute(stmt).all()]
