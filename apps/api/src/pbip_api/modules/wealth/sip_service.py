"""Fund SIPs: a monthly purchase of fund units on the ``run-fund-sips`` beat job (ADR-0058 s. 7).

Creating and cancelling a SIP moves no money. Each instalment is ONE use case in ONE database
transaction in the AGENTS.md lock order: the per-user advisory lock, the idempotency key
``fund-sip/{sip_id}/{run_on}``, the SIP row ``FOR UPDATE`` (re-checked), then exactly the purchase
path of a customer order (holding row, accounts sorted before the header). ``UNIQUE (sip_id,
run_on)`` keeps a run exactly-once after the key has expired. A refused instalment (insufficient
funds, an inactive account) rolls the purchase back and is recorded as FAILED in a second
transaction; the SIP moves to next month and missed months are never caught up.
"""

import time
from datetime import date, datetime
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
from pbip_api.core.errors import NotFoundError, UnprocessableError
from pbip_api.modules.accounts.policy import get_owned_account
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.notifications import service as notifications
from pbip_api.modules.wealth import deposit_math, investing_service
from pbip_api.modules.wealth.models import (
    FundSip,
    FundSipRun,
    InstrumentKind,
    InvestmentInstrument,
    InvestmentOrder,
    SipRunStatus,
    SipStatus,
)
from pbip_api.modules.wealth.policy import get_owned_sip, list_owned_sips
from pbip_api.modules.wealth.schemas import SipCreateRequest, SipOut, SipRunOut
from pbip_api.modules.wealth.service import (
    WealthResult,
    _begin_idempotent,
    _hash,
    _lock_user,
    _store_response,
)

logger = structlog.get_logger(__name__)

CHANNEL = "SCHEDULED"
BATCH_SIZE = 200
RUNS_SHOWN = 12


class NotAFundError(UnprocessableError):
    code = "not_a_fund"


class SipOutcome(StrEnum):
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"  # refused (funds, account): recorded, notified, moved to next month
    REPLAY = "REPLAY"  # this run's key already carries a committed instalment
    SKIPPED = "SKIPPED"  # cancelled, already run or not yet due
    ERROR = "ERROR"  # unexpected failure, rolled back and logged; retried next run


# ------------------------------------------------------------------------------ dates
def first_run_on(day_of_month: int, today: date) -> date:
    """The first instalment: this month if the day is still ahead (or today), else next month."""
    if day_of_month >= today.day:
        return today.replace(day=day_of_month)
    return deposit_math.add_months(today.replace(day=day_of_month), 1)


def next_run_after(day_of_month: int, after: date) -> date:
    """The first instalment day strictly after ``after`` (days are 1-28, so every month has one)."""
    candidate = after.replace(day=day_of_month)
    if candidate > after:
        return candidate
    return deposit_math.add_months(candidate, 1)


def run_key(sip_id: UUID, run_on: date) -> str:
    """``/`` is outside the ``Idempotency-Key`` header pattern: no client can pre-claim it."""
    return f"fund-sip/{sip_id}/{run_on.isoformat()}"


# ---------------------------------------------------------------------- customer use cases
def _runs(session: Session, sip_ids: list[UUID]) -> dict[UUID, list[SipRunOut]]:
    if not sip_ids:
        return {}
    rows = session.execute(
        select(FundSipRun, InvestmentOrder)
        .outerjoin(InvestmentOrder, InvestmentOrder.id == FundSipRun.order_id)
        .where(FundSipRun.sip_id.in_(sip_ids))
        .order_by(FundSipRun.run_on.desc())
    ).all()
    out: dict[UUID, list[SipRunOut]] = {sip_id: [] for sip_id in sip_ids}
    for run, order in rows:
        runs = out[run.sip_id]
        if len(runs) < RUNS_SHOWN:
            runs.append(
                SipRunOut(
                    run_on=run.run_on,
                    status=run.status.value,
                    order_id=run.order_id,
                    units_micro=order.units_micro if order else None,
                    price_minor=order.price_minor if order else None,
                    failure_reason=run.failure_reason,
                )
            )
    return out


def _sip_out(sip: FundSip, instrument: InvestmentInstrument, runs: list[SipRunOut]) -> SipOut:
    return SipOut(
        id=sip.id,
        instrument_id=instrument.id,
        instrument_code=instrument.code,
        instrument_name=instrument.name,
        account_id=sip.account_id,
        amount_minor=sip.amount_minor,
        day_of_month=sip.day_of_month,
        status=sip.status.value,
        next_run_on=sip.next_run_on if sip.status == SipStatus.ACTIVE else None,
        created_at=sip.created_at,
        cancelled_at=sip.cancelled_at,
        runs=runs,
    )


def _with_instruments(session: Session, sips: list[FundSip]) -> list[SipOut]:
    ids = {s.instrument_id for s in sips}
    instruments = {
        i.id: i
        for i in session.execute(
            select(InvestmentInstrument).where(InvestmentInstrument.id.in_(ids))
        ).scalars()
    }
    runs = _runs(session, [s.id for s in sips])
    return [_sip_out(s, instruments[s.instrument_id], runs[s.id]) for s in sips]


def list_sips(session: Session, customer_id: UUID) -> list[SipOut]:
    sips = list_owned_sips(session, customer_id)
    return _with_instruments(session, sips) if sips else []


def get_sip(session: Session, customer_id: UUID, sip_id: UUID) -> SipOut:
    return _with_instruments(session, [get_owned_sip(session, customer_id, sip_id)])[0]


def create_sip(
    session: Session,
    settings: Settings,
    *,
    customer_id: UUID,
    user_id: UUID,
    request: SipCreateRequest,
    idempotency_key: str,
    meta: RequestMeta,
    now: datetime,
) -> WealthResult:
    """Set up a monthly fund purchase. Moves no money: the first instalment runs on its day."""
    _lock_user(session, user_id)
    request_hash = _hash({"op": "wealth.sip.create"} | request.model_dump(mode="json"), user_id)
    idem_id, replay = _begin_idempotent(session, settings, user_id, idempotency_key, request_hash)
    if replay is not None:
        return replay

    instrument = investing_service.get_instrument(session, request.instrument_id)
    if instrument.kind != InstrumentKind.MUTUAL_FUND:
        raise NotAFundError("a SIP can only buy a mutual fund")
    today = bank_today(now)
    investing_service.price_purchase(  # same amount rules as a lump sum, checked up front
        instrument, today, amount_minor=request.amount_minor, units=None
    )
    account = get_owned_account(session, customer_id, request.account_id)
    investing_service.require_funding_account(account)
    sip = FundSip(
        customer_id=customer_id,
        user_id=user_id,
        account_id=account.id,
        instrument_id=instrument.id,
        amount_minor=request.amount_minor,
        day_of_month=request.day_of_month,
        status=SipStatus.ACTIVE,
        next_run_on=first_run_on(request.day_of_month, today),
    )
    session.add(sip)
    session.flush()
    session.refresh(sip)
    AuditService(session).record(
        user_id,
        "FUND_SIP_CREATED",
        "fund_sip",
        str(sip.id),
        ip=meta.ip,
        request_id=meta.request_id,
        details={
            "instrument_id": str(instrument.id),
            "account_id": str(account.id),
            "amount_minor": sip.amount_minor,
            "day_of_month": sip.day_of_month,
            "next_run_on": sip.next_run_on.isoformat(),
        },
    )
    outcome = WealthResult(201, _sip_out(sip, instrument, []).model_dump(mode="json"))
    _store_response(session, idem_id, outcome, None)
    session.commit()
    return outcome


def cancel_sip(
    session: Session, *, customer_id: UUID, user_id: UUID, sip_id: UUID, meta: RequestMeta
) -> SipOut:
    """Stop future instalments. Cancelling a cancelled SIP changes nothing (safe to retry)."""
    _lock_user(session, user_id)
    sip = get_owned_sip(session, customer_id, sip_id, for_update=True)
    if sip.status == SipStatus.ACTIVE:
        sip.status = SipStatus.CANCELLED
        sip.cancelled_at = utcnow()
        session.flush()
        AuditService(session).record(
            user_id,
            "FUND_SIP_CANCELLED",
            "fund_sip",
            str(sip.id),
            ip=meta.ip,
            request_id=meta.request_id,
            details={"instrument_id": str(sip.instrument_id)},
        )
    out = _with_instruments(session, [sip])[0]
    session.commit()
    return out


# --------------------------------------------------------------------------- the runner
def _locked_due_sip(session: Session, sip_id: UUID, run_on: date, today: date) -> FundSip | None:
    """The SIP row ``FOR UPDATE``, or None unless it is still active and due on ``run_on``."""
    sip = session.execute(
        select(FundSip)
        .where(FundSip.id == sip_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if sip is None or sip.status != SipStatus.ACTIVE or sip.next_run_on != run_on or run_on > today:
        return None
    return sip


def _execute(
    session: Session, settings: Settings, sip_id: UUID, user_id: UUID, run_on: date, now: datetime
) -> SipOutcome:
    _lock_user(session, user_id)
    request_hash = _hash(
        {"op": "wealth.sip.run", "sip_id": str(sip_id), "run_on": run_on.isoformat()}, user_id
    )
    idem_id, replay = _begin_idempotent(
        session, settings, user_id, run_key(sip_id, run_on), request_hash
    )
    if replay is not None:
        session.rollback()
        return SipOutcome.REPLAY
    today = bank_today(now)
    sip = _locked_due_sip(session, sip_id, run_on, today)
    if sip is None:
        session.rollback()  # also drops the key claimed above: nothing was bought
        return SipOutcome.SKIPPED
    instrument = investing_service.get_instrument(session, sip.instrument_id)
    purchase = investing_service.price_purchase(
        instrument, today, amount_minor=sip.amount_minor, units=None
    )
    account = get_owned_account(session, sip.customer_id, sip.account_id)
    order = investing_service.execute_purchase(
        session,
        customer_id=sip.customer_id,
        user_id=sip.user_id,
        account=account,
        purchase=purchase,
        today=today,
        channel=CHANNEL,
        sip_id=sip.id,
    )
    session.add(
        FundSipRun(sip_id=sip.id, run_on=run_on, status=SipRunStatus.EXECUTED, order_id=order.id)
    )
    sip.next_run_on = next_run_after(sip.day_of_month, today)
    session.flush()
    investing_service.notify_order(session, notifications.INVESTMENT_BOUGHT, sip.user_id, order)
    body: dict[str, Any] = {
        "sip_id": str(sip.id),
        "run_on": run_on.isoformat(),
        "order_id": str(order.id),
        "transaction_id": str(order.transaction_id),
        "amount_minor": order.amount_minor,
        "units_micro": order.units_micro,
    }
    AuditService(session).record(
        None,
        "FUND_SIP_INSTALMENT_BOUGHT",
        "fund_sip",
        str(sip.id),
        details={"actor": "scheduler", "channel": CHANNEL, **body},
    )
    _store_response(session, idem_id, WealthResult(201, body), order.transaction_id)
    session.commit()
    return SipOutcome.EXECUTED


def _record_failure(
    session: Session, sip_id: UUID, user_id: UUID, run_on: date, now: datetime, reason: str
) -> SipOutcome:
    """A fresh transaction after the instalment rolled back: record it, notify, move on."""
    _lock_user(session, user_id)
    today = bank_today(now)
    sip = _locked_due_sip(session, sip_id, run_on, today)
    if sip is None:
        session.rollback()
        return SipOutcome.SKIPPED
    session.add(
        FundSipRun(sip_id=sip.id, run_on=run_on, status=SipRunStatus.FAILED, failure_reason=reason)
    )
    sip.next_run_on = next_run_after(sip.day_of_month, today)
    session.flush()
    notifications.enqueue(
        session,
        notifications.FUND_SIP_INSTALMENT_FAILED,
        {
            "user_id": str(sip.user_id),
            "account_id": str(sip.account_id),
            "amount_minor": sip.amount_minor,
            "currency": "INR",
            "run_on": run_on.isoformat(),
            "next_run_on": sip.next_run_on.isoformat(),
        },
    )
    AuditService(session).record(
        None,
        "FUND_SIP_INSTALMENT_FAILED",
        "fund_sip",
        str(sip.id),
        details={"actor": "scheduler", "run_on": run_on.isoformat(), "reason": reason},
    )
    session.commit()
    return SipOutcome.FAILED


def run_sip(session: Session, settings: Settings, sip_id: UUID, now: datetime) -> SipOutcome:
    """Run one due instalment, at most once (ADR-0058 section 7)."""
    row = session.execute(
        select(FundSip.user_id, FundSip.next_run_on).where(FundSip.id == sip_id)
    ).one_or_none()
    session.rollback()  # end the read-only snapshot: the use case starts with the user lock
    if row is None:
        return SipOutcome.SKIPPED
    user_id, run_on = row
    try:
        return _execute(session, settings, sip_id, user_id, run_on, now)
    except NotFoundError:
        session.rollback()  # the account is no longer the customer's
        return _record_failure(session, sip_id, user_id, run_on, now, "account_unavailable")
    except UnprocessableError as exc:  # insufficient funds, inactive account, amount rules
        session.rollback()
        return _record_failure(session, sip_id, user_id, run_on, now, exc.code[:40])


def due_sip_ids(session: Session, now: datetime, limit: int) -> list[UUID]:
    stmt = (
        select(FundSip.id)
        .where(FundSip.status == SipStatus.ACTIVE, FundSip.next_run_on <= bank_today(now))
        .order_by(FundSip.next_run_on, FundSip.id)
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


def process_due_sips(
    engine: Engine | None = None,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> dict[str, int]:
    """The beat job: each SIP in its own session and transaction, so one failure never rolls
    back or blocks another. ``now`` is injected by tests; the task passes the wall clock."""
    started = time.monotonic()
    db_engine = engine or get_engine()
    config = settings or get_settings()
    moment = now or utcnow()
    counts = {outcome.value: 0 for outcome in SipOutcome}
    with Session(db_engine) as session:
        ids = due_sip_ids(session, moment, BATCH_SIZE)
    for sip_id in ids:
        with Session(db_engine, expire_on_commit=False) as session:
            try:
                outcome = run_sip(session, config, sip_id, moment)
            except Exception:  # one SIP must never stop the batch; retried next run
                logger.exception("fund_sip_run_failed", sip_id=str(sip_id))
                session.rollback()
                outcome = SipOutcome.ERROR
        counts[outcome.value] += 1
    logger.info(
        "fund_sip_run",
        due=len(ids),
        duration_ms=round((time.monotonic() - started) * 1000),
        **counts,
    )
    return counts
