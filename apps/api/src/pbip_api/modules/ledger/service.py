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
from datetime import datetime
from uuid import UUID

from sqlalchemy import case, delete, func, select
from sqlalchemy.orm import Session

from pbip_api.core.config import get_settings
from pbip_api.core.db import utcnow
from pbip_api.core.errors import ConflictError, NotFoundError, UnprocessableError
from pbip_api.modules.accounts.models import Account, AccountStatus, AccountType
from pbip_api.modules.ledger.models import (
    EntryDirection,
    Hold,
    HoldStatus,
    IdempotencyKey,
    Transaction,
    TransactionEntry,
    TransactionStatus,
    TransactionType,
)

SYSTEM_CLEARING_ID = UUID("00000000-0000-0000-0000-0000000000c1")
SYSTEM_FEES_ID = UUID("00000000-0000-0000-0000-0000000000c2")
# The bank's liability for money held on fixed deposit (migration 0018, ADR-0025).
SYSTEM_FIXED_DEPOSITS_ID = UUID("00000000-0000-0000-0000-0000000000c3")
# The settlement counterparty of simulated fund, stock and bond orders (migration 0031, ADR-0058).
SYSTEM_INVESTMENTS_ID = UUID("00000000-0000-0000-0000-0000000000c4")


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


def initialize_account(account: Account) -> None:
    """Register a newly opened account with the ledger at a zero materialised balance.

    An account's balance is, by definition, the sum of its entries; a new account has none, so its
    balance is zero and nothing is posted. A zero-value "opening" transaction is impossible by
    design (``CHECK amount > 0`` on headers and entries) and would fabricate an audit-trail event
    that moves no money. Called before the account row is first flushed.
    """
    account.ledger_balance = 0
    account.held_amount = 0


def entry_balance(session: Session, account_id: UUID) -> int:
    """The balance recomputed from the immutable entries (CREDIT - DEBIT).

    Reconciliation oracle: for every account it must equal the materialised ``ledger_balance``.
    """
    signed = case(
        (TransactionEntry.direction == EntryDirection.CREDIT, TransactionEntry.amount),
        else_=-TransactionEntry.amount,
    )
    total = session.execute(
        select(func.coalesce(func.sum(signed), 0)).where(TransactionEntry.account_id == account_id)
    ).scalar_one()
    return int(total)


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
    at: datetime | None = None,
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
    # ``at`` back-dates a deposit (``deposit(posted_at=...)``). Otherwise the app clock stamps the
    # header, not the database's ``now()``: fraud rules compare ``created_at`` with the scorer's
    # app-clock ``now``, and the database host's clock can run ahead of it (Docker Desktop's VM
    # drifts by hundreds of ms), which read 181 dormant days as 180 (#442, as #426 for #371).
    transaction.created_at = at if at is not None else utcnow()
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


def post_transaction(
    session: Session, transaction: Transaction, legs: list[Leg], *, at: datetime | None = None
) -> None:
    """Write balanced, immutable entries and update running balances atomically.

    ``at`` dates the entries and ``posted_at`` (only ``deposit(posted_at=...)`` passes it, after
    its checks); without it the entries take the database's now() and ``posted_at`` the clock.
    """
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
        entry = TransactionEntry(
            transaction_id=transaction.id,
            account_id=account.id,
            direction=leg.direction,
            amount=leg.amount,
            currency=transaction.currency,
            balance_after=account.ledger_balance,
        )
        if at is not None:
            entry.created_at = at
        session.add(entry)
    transaction.status = TransactionStatus.POSTED
    transaction.posted_at = at or utcnow()
    session.flush()


def deposit(
    session: Session,
    account_id: UUID,
    amount: int,
    currency: str = "INR",
    reference: str = "Deposit",
    *,
    posted_at: datetime | None = None,
) -> Transaction:
    """Fund an account from the internal clearing account (used by seeding and tests).

    ``posted_at`` back-dates the deposit (header and both entries) so the demo seed can build a
    salary history (ADR-0048). It is refused in staging/production, in the future, and before
    the account's latest entry: every ``balance_after`` must stay the running balance in time
    order, which statements and daily balances rely on.
    """
    if posted_at is not None:
        lock_accounts(session, [SYSTEM_CLEARING_ID, account_id])  # sorted, before the header
        _check_backdate(session, account_id, posted_at)
    transaction = create_transaction(
        session,
        type_=TransactionType.DEPOSIT,
        amount=amount,
        currency=currency,
        debit_account_id=SYSTEM_CLEARING_ID,
        credit_account_id=account_id,
        reference=reference,
        at=posted_at,
    )
    post_transaction(session, transaction, transfer_legs(transaction), at=posted_at)
    return transaction


def seed_payment(
    session: Session,
    account_id: UUID,
    amount: int,
    *,
    reference: str,
    posted_at: datetime,
    payee_id: UUID | None = None,
    initiated_by_user_id: UUID | None = None,
    currency: str = "INR",
) -> Transaction:
    """A posted payment from a customer account to an external payee, dated ``posted_at``: the
    demo seed's spending history (ADR-0048). Only ``scripts/seed_dev.py`` calls it.

    The external rail is simulated through the clearing account, exactly as a live transfer to
    another bank's payee is. The same guards as ``deposit(posted_at=...)`` apply, and the usual
    balance check: a payment the account cannot afford is refused. It is history, not a live
    instruction, so it is not fraud-scored and has no ``fraud_events`` row (scoring is a
    decision about a payment being made now; ``payments/service.py`` remains the only path for
    those). Later live transfers do read it as the account's history.
    """
    lock_accounts(session, [SYSTEM_CLEARING_ID, account_id])  # sorted, before the header
    _check_backdate(session, account_id, posted_at)
    transaction = create_transaction(
        session,
        type_=TransactionType.TRANSFER,
        amount=amount,
        currency=currency,
        debit_account_id=account_id,
        credit_account_id=SYSTEM_CLEARING_ID,
        initiated_by_user_id=initiated_by_user_id,
        payee_id=payee_id,
        reference=reference,
        at=posted_at,
    )
    post_transaction(session, transaction, transfer_legs(transaction), at=posted_at)
    return transaction


def _check_backdate(session: Session, account_id: UUID, posted_at: datetime) -> None:
    """Refuse a back-dated posting that would not be a faithful history (ADR-0048).

    The account is already locked, so no entry can be added between this check and the posting.
    The clearing account is not checked: it is a system account whose running balance is never
    shown by date.
    """
    if get_settings().is_deployed:
        raise LedgerInvariantError("back-dated postings are refused in staging/production")
    if posted_at.tzinfo is None:
        raise LedgerInvariantError("posted_at must be timezone-aware")
    if posted_at > utcnow():
        raise LedgerInvariantError("posted_at must not be in the future")
    latest = session.execute(
        select(func.max(TransactionEntry.created_at)).where(
            TransactionEntry.account_id == account_id
        )
    ).scalar_one()
    if latest is not None and posted_at < latest:
        raise LedgerInvariantError("posted_at must not precede the account's latest entry")


def disburse_loan(
    session: Session,
    *,
    account_id: UUID,
    amount: int,
    currency: str,
    initiated_by_user_id: UUID,
    reference: str,
) -> Transaction:
    """Pay out an approved loan: DEBIT the bank's clearing account, CREDIT the customer (ADR-0024).

    Its own type (never DEPOSIT or TRANSFER) keeps borrowed money out of the customer's ledger
    "income" (``analytics.repository.INCOME_TYPES``), which eligibility reads. Locks both
    accounts in sorted order before the header is inserted (``create_transaction``).
    """
    transaction = create_transaction(
        session,
        type_=TransactionType.LOAN_DISBURSAL,
        amount=amount,
        currency=currency,
        debit_account_id=SYSTEM_CLEARING_ID,
        credit_account_id=account_id,
        initiated_by_user_id=initiated_by_user_id,
        reference=reference,
    )
    post_transaction(session, transaction, transfer_legs(transaction))
    return transaction


def collect_loan_repayment(
    session: Session,
    *,
    account_id: UUID,
    amount: int,
    currency: str,
    initiated_by_user_id: UUID,
    reference: str,
) -> Transaction:
    """Collect loan instalments: DEBIT the customer, CREDIT the bank's clearing account (ADR-0024).

    Raises ``InsufficientFundsError`` / ``AccountNotActiveError`` like any debit; the caller rolls
    back and the scheduler retries (missed-repayment rules in ADR-0024).
    """
    transaction = create_transaction(
        session,
        type_=TransactionType.LOAN_REPAYMENT,
        amount=amount,
        currency=currency,
        debit_account_id=account_id,
        credit_account_id=SYSTEM_CLEARING_ID,
        initiated_by_user_id=initiated_by_user_id,
        reference=reference,
        channel="SCHEDULED",
    )
    post_transaction(session, transaction, transfer_legs(transaction))
    return transaction


def book_fixed_deposit(
    session: Session,
    *,
    account_id: UUID,
    amount: int,
    currency: str,
    initiated_by_user_id: UUID,
    reference: str,
) -> Transaction:
    """Move a deposit's principal: DEBIT the customer, CREDIT ``SYS-FIXED-DEPOSITS`` (ADR-0025).

    A real posting, not a hold: the money leaves the customer's balance for the whole term. Raises
    ``InsufficientFundsError`` like any debit. Locks both accounts (sorted) before the header.
    """
    transaction = create_transaction(
        session,
        type_=TransactionType.FD_BOOKING,
        amount=amount,
        currency=currency,
        debit_account_id=account_id,
        credit_account_id=SYSTEM_FIXED_DEPOSITS_ID,
        initiated_by_user_id=initiated_by_user_id,
        reference=reference,
    )
    post_transaction(session, transaction, transfer_legs(transaction))
    return transaction


@dataclass(frozen=True, slots=True)
class DepositPayout:
    principal: Transaction
    interest: Transaction | None  # None when no interest is due (e.g. closed in the first week)


def pay_out_fixed_deposit(
    session: Session,
    *,
    account_id: UUID,
    principal: int,
    interest: int,
    currency: str,
    initiated_by_user_id: UUID,
    reference: str,
    channel: str = "WEB",
) -> DepositPayout:
    """Close a deposit (ADR-0025): the principal back from ``SYS-FIXED-DEPOSITS`` (``FD_CLOSURE``)
    and the interest from the bank's clearing account (``FD_INTEREST``), two balanced postings.

    ALL THREE accounts are locked in sorted id order before either header is inserted: locking the
    clearing account only for the second posting would take a row lock after the first header's
    KEY SHARE locks, the order the lock contract forbids. ``channel`` is ``SCHEDULED`` for the
    automatic maturity payout (ADR-0029).
    """
    if interest < 0:
        raise LedgerInvariantError("interest cannot be negative")
    lock_accounts(session, [account_id, SYSTEM_FIXED_DEPOSITS_ID, SYSTEM_CLEARING_ID])
    returned = create_transaction(
        session,
        type_=TransactionType.FD_CLOSURE,
        amount=principal,
        currency=currency,
        debit_account_id=SYSTEM_FIXED_DEPOSITS_ID,
        credit_account_id=account_id,
        initiated_by_user_id=initiated_by_user_id,
        reference=f"{reference} principal",
        channel=channel,
    )
    post_transaction(session, returned, transfer_legs(returned))
    if interest == 0:
        return DepositPayout(returned, None)
    paid = create_transaction(
        session,
        type_=TransactionType.FD_INTEREST,
        amount=interest,
        currency=currency,
        debit_account_id=SYSTEM_CLEARING_ID,
        credit_account_id=account_id,
        initiated_by_user_id=initiated_by_user_id,
        reference=f"{reference} interest",
        channel=channel,
    )
    post_transaction(session, paid, transfer_legs(paid))
    return DepositPayout(returned, paid)


def buy_investment(
    session: Session,
    *,
    account_id: UUID,
    amount: int,
    currency: str,
    initiated_by_user_id: UUID,
    reference: str,
    channel: str = "WEB",
) -> Transaction:
    """Pay for a simulated fund, stock or bond order: DEBIT the customer, CREDIT
    ``SYS-INVESTMENTS`` (``INVESTMENT_PURCHASE``, ADR-0058 section 4).

    Raises ``InsufficientFundsError`` like any debit. Locks both accounts (sorted) before the
    header. ``channel`` is ``SCHEDULED`` for a fund SIP instalment.
    """
    transaction = create_transaction(
        session,
        type_=TransactionType.INVESTMENT_PURCHASE,
        amount=amount,
        currency=currency,
        debit_account_id=account_id,
        credit_account_id=SYSTEM_INVESTMENTS_ID,
        initiated_by_user_id=initiated_by_user_id,
        reference=reference,
        channel=channel,
    )
    post_transaction(session, transaction, transfer_legs(transaction))
    return transaction


def sell_investment(
    session: Session,
    *,
    account_id: UUID,
    amount: int,
    currency: str,
    initiated_by_user_id: UUID,
    reference: str,
) -> Transaction:
    """Pay out a simulated sale: DEBIT ``SYS-INVESTMENTS``, CREDIT the customer
    (``INVESTMENT_SALE``, ADR-0058 section 4). The system account may go below zero when the
    proceeds exceed what was paid in (the simulated market pays the gain), as clearing does.
    """
    transaction = create_transaction(
        session,
        type_=TransactionType.INVESTMENT_SALE,
        amount=amount,
        currency=currency,
        debit_account_id=SYSTEM_INVESTMENTS_ID,
        credit_account_id=account_id,
        initiated_by_user_id=initiated_by_user_id,
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


@dataclass(frozen=True)
class EntryFilters:
    """Optional narrowing of an account's entries (TASK-017). Every field is already validated."""

    start: datetime | None = None  # inclusive
    end: datetime | None = None  # exclusive
    min_amount: int | None = None
    max_amount: int | None = None
    direction: EntryDirection | None = None
    text: str | None = None  # case-insensitive substring of the payment reference


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_account_entries(
    session: Session,
    account_id: UUID,
    limit: int,
    after_seq: int | None,
    newer: bool = False,
    filters: EntryFilters | None = None,
) -> list[tuple[TransactionEntry, Transaction]]:
    """Keyset page of an account's entries by posting order (``seq``).

    ``newer=False`` walks back in time (newest first, ``seq < after_seq``); ``newer=True`` walks
    forward (oldest first, ``seq > after_seq``). Filters narrow the same keyset walk, so a cursor
    from a filtered page continues that filtered list. Values are bound parameters, never SQL text.
    The caller authorizes the account first.
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
    f = filters or EntryFilters()
    if f.start is not None:
        stmt = stmt.where(TransactionEntry.created_at >= f.start)
    if f.end is not None:
        stmt = stmt.where(TransactionEntry.created_at < f.end)
    if f.min_amount is not None:
        stmt = stmt.where(TransactionEntry.amount >= f.min_amount)
    if f.max_amount is not None:
        stmt = stmt.where(TransactionEntry.amount <= f.max_amount)
    if f.direction is not None:
        stmt = stmt.where(TransactionEntry.direction == f.direction)
    if f.text:
        stmt = stmt.where(Transaction.reference.ilike(f"%{_escape_like(f.text)}%", escape="\\"))
    return [(entry, txn) for entry, txn in session.execute(stmt).all()]


@dataclass(frozen=True)
class PeriodEntry:
    """One posted entry with its transaction's description fields (statements, read-only)."""

    direction: EntryDirection
    amount: int
    balance_after: int
    created_at: datetime
    type: TransactionType
    status: TransactionStatus
    reference: str | None


def entries_in_period(
    session: Session, account_id: UUID, start: datetime, end: datetime, limit: int
) -> list[PeriodEntry]:
    """Read-only: an account's entries posted in ``[start, end)``, oldest first (statements).

    Selects plain columns, not ORM entities: a year of entries is read without hydrating and
    identity-mapping thousands of objects. Uses ``ix_transaction_entries_account_id_created_at``.
    The caller authorizes the account.
    """
    stmt = (
        select(
            TransactionEntry.direction,
            TransactionEntry.amount,
            TransactionEntry.balance_after,
            TransactionEntry.created_at,
            Transaction.type,
            Transaction.status,
            Transaction.reference,
        )
        .join(Transaction, Transaction.id == TransactionEntry.transaction_id)
        .where(
            TransactionEntry.account_id == account_id,
            TransactionEntry.created_at >= start,
            TransactionEntry.created_at < end,
        )
        .order_by(TransactionEntry.seq.asc())
        .limit(limit)
    )
    return [PeriodEntry(*row) for row in session.execute(stmt).all()]


def balance_before(session: Session, account_id: UUID, moment: datetime) -> int:
    """Read-only: the ledger balance just before ``moment`` (0 if nothing was posted earlier)."""
    value = session.execute(
        select(TransactionEntry.balance_after)
        .where(TransactionEntry.account_id == account_id, TransactionEntry.created_at < moment)
        .order_by(TransactionEntry.seq.desc())
        .limit(1)
    ).scalar_one_or_none()
    return int(value) if value is not None else 0


# ------------------------------------------------------------------------- idempotency keys
# The keys live with the ledger because they make its postings exactly-once (ADR-0003). A key is
# answered for ``IDEMPOTENCY_TTL_HOURS`` after its first use and is unknown afterwards (ADR-0036).


def discard_expired_idempotency_key(session: Session, user_id: UUID, key: str) -> None:
    """Forget this user's key if it has expired, so the caller's claim treats it as unused.

    Called by every ``_begin_idempotent`` AFTER the per-user advisory lock, so no other request
    of this user can be reading or claiming the same key meanwhile."""
    session.execute(
        delete(IdempotencyKey).where(
            IdempotencyKey.user_id == user_id,
            IdempotencyKey.key == key,
            IdempotencyKey.expires_at <= utcnow(),
        )
    )


def purge_expired_idempotency_keys(session: Session, now: datetime, limit: int) -> int:
    """Delete up to ``limit`` expired keys and return how many; the caller commits.

    ``SKIP LOCKED``: a row a live request is discarding (under its user lock) is left for the
    next run instead of blocking it. Deleting a key never touches the transaction it answered."""
    expired = (
        select(IdempotencyKey.id)
        .where(IdempotencyKey.expires_at <= now)
        .order_by(IdempotencyKey.expires_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    result = session.execute(
        delete(IdempotencyKey).where(IdempotencyKey.id.in_(expired)).returning(IdempotencyKey.id)
    )
    return len(result.all())


# Customer-initiated outgoing payments: the population the fraud scorer sees (transfers, scheduled
# transfers and bill payments). INITIATED never survives a commit, so every stored row was scored.
SCORED_PAYMENT_TYPES = (TransactionType.TRANSFER, TransactionType.BILL_PAYMENT)


@dataclass(frozen=True, slots=True)
class PaymentObservation:
    """One scored outgoing payment, reduced to what drift monitoring needs (no reference text)."""

    amount: int
    debit_account_id: UUID
    created_at: datetime


def scored_payments_between(
    session: Session, start: datetime, end: datetime
) -> list[PaymentObservation]:
    """Read-only: every scored outgoing payment created in ``[start, end)``, oldest first.

    Feeds the PSI drift job (ADR-0038). Every outcome counts (posted, declined, held, reversed):
    the scorer saw all of them, so all of them are its input distribution. Plain columns, no ORM
    entities hydrated; an hourly background read, never on a request.
    """
    stmt = (
        select(Transaction.amount, Transaction.debit_account_id, Transaction.created_at)
        .where(
            Transaction.type.in_(SCORED_PAYMENT_TYPES),
            Transaction.status != TransactionStatus.INITIATED,
            Transaction.debit_account_id.is_not(None),
            Transaction.created_at >= start,
            Transaction.created_at < end,
        )
        .order_by(Transaction.created_at.asc(), Transaction.id.asc())
    )
    return [
        PaymentObservation(int(amount), account_id, created_at)
        for amount, account_id, created_at in session.execute(stmt).all()
        if account_id is not None  # already filtered in SQL; narrows the type
    ]
