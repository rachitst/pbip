"""Automatic payout of fixed deposits at maturity (issue #332, ADR-0029).

Each matured deposit is ONE use case in ONE database transaction, in the AGENTS.md lock order:
the per-user advisory lock, the per-deposit idempotency key, the deposit row ``FOR UPDATE``
(re-checked), then the accounts in sorted id order inside ``ledger.pay_out_fixed_deposit``. The
interest comes from the same ``_closure_interest`` a customer closure uses, so the maths is never
duplicated. A source account that is not active holds the payout: the money stays on
``SYS-FIXED-DEPOSITS``, the customer is notified once, and every run retries until the account is
active again.
"""

import time
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pbip_api.core.calendar import bank_today
from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import get_engine, utcnow
from pbip_api.core.metrics import FD_MATURITY_PAYOUTS_TOTAL
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.notifications import service as notifications
from pbip_api.modules.wealth import service as wealth
from pbip_api.modules.wealth.models import ClosureKind, FixedDeposit, FixedDepositStatus

logger = structlog.get_logger(__name__)

CHANNEL = "SCHEDULED"
HOLD_REASON = "account_not_active"


class PayoutOutcome(StrEnum):
    PAID = "PAID"
    HELD = "HELD"  # first time the account was found not active: audited and notified
    STILL_HELD = "STILL_HELD"  # held before; retried, nothing new written
    REPLAY = "REPLAY"  # this deposit's key already carries a committed payout
    SKIPPED = "SKIPPED"  # closed meanwhile (early, manual or another run) or not yet matured
    ERROR = "ERROR"  # unexpected failure, rolled back and logged; retried next run


def payout_key(deposit_id: UUID) -> str:
    """One key per deposit. ``/`` is outside the ``Idempotency-Key`` header pattern, so no client
    request can pre-claim or collide with it."""
    return f"fd-maturity/{deposit_id}"


def _locked_due_deposit(session: Session, deposit_id: UUID, now: datetime) -> FixedDeposit | None:
    """The deposit row ``FOR UPDATE``, or None unless it is still open and matured."""
    deposit = session.execute(
        select(FixedDeposit)
        .where(FixedDeposit.id == deposit_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if (
        deposit is None
        or deposit.status != FixedDepositStatus.ACTIVE
        or deposit.maturity_date > bank_today(now)
    ):
        return None
    return deposit


def _pay(
    session: Session, settings: Settings, deposit_id: UUID, user_id: UUID, now: datetime
) -> PayoutOutcome:
    wealth._lock_user(session, user_id)
    request_hash = wealth._hash(
        {"op": "wealth.fd.maturity_payout", "deposit_id": str(deposit_id)}, user_id
    )
    idem_id, replay = wealth._begin_idempotent(
        session, settings, user_id, payout_key(deposit_id), request_hash
    )
    if replay is not None:
        session.rollback()
        return PayoutOutcome.REPLAY
    deposit = _locked_due_deposit(session, deposit_id, now)
    if deposit is None:
        session.rollback()  # also drops the key claimed above: nothing was paid
        return PayoutOutcome.SKIPPED
    today = bank_today(now)
    closure = wealth._closure_interest(deposit, today)
    if closure is None or closure[0] != ClosureKind.MATURED:
        raise ledger.LedgerInvariantError("a matured open deposit must close as MATURED")
    interest = closure[1]
    name = wealth._product_names(session, {deposit.product_id})[deposit.product_id]
    payout = ledger.pay_out_fixed_deposit(
        session,
        account_id=deposit.account_id,
        principal=deposit.principal,
        interest=interest,
        currency=deposit.currency,
        initiated_by_user_id=deposit.user_id,
        reference=f"{name} matured",
        channel=CHANNEL,
    )
    deposit.status = FixedDepositStatus.CLOSED
    deposit.closure_kind = ClosureKind.MATURED
    deposit.paid_interest = interest
    deposit.closed_on = today
    deposit.closed_at = now
    deposit.closure_transaction_id = payout.principal.id
    deposit.interest_transaction_id = payout.interest.id if payout.interest else None
    deposit.payout_held_at = None
    session.flush()
    body: dict[str, Any] = {
        "deposit_id": str(deposit.id),
        "transaction_id": str(payout.principal.id),
        "interest_transaction_id": str(payout.interest.id) if payout.interest else None,
        "principal_minor": deposit.principal,
        "interest_minor": interest,
        "currency": deposit.currency,
    }
    notifications.enqueue(
        session,
        notifications.FIXED_DEPOSIT_MATURED,
        {
            "user_id": str(deposit.user_id),
            "transaction_id": str(payout.principal.id),
            "account_id": str(deposit.account_id),
            "amount_minor": deposit.principal + interest,
            "interest_minor": interest,
            "currency": deposit.currency,
        },
    )
    AuditService(session).record(
        None,
        "FIXED_DEPOSIT_MATURED_PAID_OUT",
        "fixed_deposit",
        str(deposit.id),
        details={"actor": "scheduler", "channel": CHANNEL, **body},
    )
    wealth._store_response(session, idem_id, wealth.WealthResult(200, body), payout.principal.id)
    session.commit()
    return PayoutOutcome.PAID


def _hold(session: Session, deposit_id: UUID, user_id: UUID, now: datetime) -> PayoutOutcome:
    """A fresh transaction after the payout rolled back: mark the hold and notify, once."""
    wealth._lock_user(session, user_id)
    deposit = _locked_due_deposit(session, deposit_id, now)
    if deposit is None:
        session.rollback()
        return PayoutOutcome.SKIPPED
    if deposit.payout_held_at is not None:
        session.rollback()
        return PayoutOutcome.STILL_HELD
    deposit.payout_held_at = now
    session.flush()
    notifications.enqueue(
        session,
        notifications.FIXED_DEPOSIT_PAYOUT_HELD,
        {
            "user_id": str(deposit.user_id),
            "account_id": str(deposit.account_id),
            "amount_minor": deposit.maturity_amount,
            "currency": deposit.currency,
        },
    )
    AuditService(session).record(
        None,
        "FIXED_DEPOSIT_PAYOUT_HELD",
        "fixed_deposit",
        str(deposit.id),
        details={
            "actor": "scheduler",
            "reason": HOLD_REASON,
            "account_id": str(deposit.account_id),
            "amount_minor": deposit.maturity_amount,
        },
    )
    session.commit()
    return PayoutOutcome.HELD


def pay_out_matured_deposit(
    session: Session, settings: Settings, deposit_id: UUID, now: datetime
) -> PayoutOutcome:
    """Pay one matured deposit to its source account, at most once (ADR-0029 sections 2-4)."""
    user_id = session.execute(
        select(FixedDeposit.user_id).where(FixedDeposit.id == deposit_id)
    ).scalar_one_or_none()
    if user_id is None:
        return PayoutOutcome.SKIPPED
    session.rollback()  # end the read-only snapshot: the use case starts with the user lock
    try:
        return _pay(session, settings, deposit_id, user_id, now)
    except ledger.AccountNotActiveError:
        session.rollback()  # no posting, no key: the deposit stays open on SYS-FIXED-DEPOSITS
        return _hold(session, deposit_id, user_id, now)


def due_deposit_ids(session: Session, now: datetime, limit: int) -> list[UUID]:
    """Matured open deposits; never-held first so held ones cannot starve new ones."""
    stmt = (
        select(FixedDeposit.id)
        .where(
            FixedDeposit.status == FixedDepositStatus.ACTIVE,
            FixedDeposit.maturity_date <= bank_today(now),
        )
        .order_by(
            FixedDeposit.payout_held_at.asc().nulls_first(),
            FixedDeposit.maturity_date,
            FixedDeposit.id,
        )
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


def process_matured_deposits(
    engine: Engine | None = None,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> dict[str, int]:
    """The beat job: each deposit in its own session and transaction, so one failure never rolls
    back or blocks another. ``now`` is injected by tests; the task passes the wall clock."""
    started = time.monotonic()
    db_engine = engine or get_engine()
    config = settings or get_settings()
    moment = now or utcnow()
    counts = {outcome.value: 0 for outcome in PayoutOutcome}
    with Session(db_engine) as session:
        ids = due_deposit_ids(session, moment, config.fd_maturity_payout_batch_size)
    for deposit_id in ids:
        with Session(db_engine, expire_on_commit=False) as session:
            try:
                outcome = pay_out_matured_deposit(session, config, deposit_id, moment)
            except Exception:  # one deposit must never stop the batch; retried next run
                logger.exception("fd_maturity_payout_failed", deposit_id=str(deposit_id))
                session.rollback()
                outcome = PayoutOutcome.ERROR
        counts[outcome.value] += 1
        FD_MATURITY_PAYOUTS_TOTAL.labels(outcome=outcome.value).inc()
    logger.info(
        "fd_maturity_payout_run",
        due=len(ids),
        duration_ms=round((time.monotonic() - started) * 1000),
        **counts,
    )
    return counts
