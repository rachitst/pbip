"""Wealth catalogue, deposit quotes, booking, closure and the portfolio (TASK-063/064, ADR-0025).

Booking and closing are each ONE use case and ONE database transaction. Lock order (AGENTS.md
section 4): the per-user advisory lock, then the idempotency key, then (closure only) the deposit
row, then the accounts ``FOR UPDATE`` in sorted id order inside the ledger BEFORE any transaction
header or ``fixed_deposits`` row is inserted. Rates come only from the seeded catalogue, maturity
figures only from ``deposit_math``; "matured", accrued interest and the portfolio are computed on
read.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from pbip_api.core.calendar import bank_today
from pbip_api.core.config import Settings
from pbip_api.core.db import utcnow
from pbip_api.core.errors import ConflictError, NotFoundError, UnprocessableError
from pbip_api.core.metrics import IDEMPOTENT_REPLAYS_TOTAL
from pbip_api.modules.accounts import service as accounts_service
from pbip_api.modules.accounts.models import AccountStatus
from pbip_api.modules.accounts.policy import get_owned_account
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import IdempotencyKey
from pbip_api.modules.notifications import service as notifications
from pbip_api.modules.payments.service import IdempotencyKeyReuseError
from pbip_api.modules.wealth import deposit_math, insurance, pricing
from pbip_api.modules.wealth.models import (
    ClosureKind,
    FixedDeposit,
    FixedDepositStatus,
    InstrumentKind,
    InsuranceProduct,
    InsuranceType,
    InvestmentInstrument,
    ProductType,
    RiskLevel,
    WealthProduct,
    YieldBasis,
)
from pbip_api.modules.wealth.policy import (
    get_owned_fixed_deposit,
    list_owned_fixed_deposits,
    list_owned_holdings,
)
from pbip_api.modules.wealth.schemas import (
    AgeBandHintOut,
    AllocationSlice,
    DepositBookRequest,
    DepositQuoteOut,
    DepositTermsRequest,
    FixedDepositOut,
    IndicativePremiumOut,
    InsuranceEligibilityOut,
    InsuranceProductOut,
    PortfolioOut,
    WealthProductOut,
)

CURRENCY = "INR"
FULL_SHARE_BPS = 10_000


class ProductNotBookableError(UnprocessableError):
    code = "product_not_bookable"


class DepositLockedInError(UnprocessableError):
    code = "deposit_locked_in"


class DepositClosedError(ConflictError):
    code = "deposit_closed"


# ------------------------------------------------------------------ catalogue
def _product_apy(product: WealthProduct) -> int | None:
    if product.annual_rate_bps is None:
        return None
    if product.yield_basis == YieldBasis.GUARANTEED:
        return deposit_math.apy_bps(product.annual_rate_bps)
    return product.annual_rate_bps  # a coupon is paid out, not compounded


def _product_out(product: WealthProduct, investable: bool) -> WealthProductOut:
    return WealthProductOut(
        id=product.id,
        code=product.code,
        product_type=product.product_type.value,
        name=product.name,
        description=product.description,
        risk_level=product.risk_level.value,
        yield_basis=product.yield_basis.value,
        annual_rate_bps=product.annual_rate_bps,
        apy_bps=_product_apy(product),
        tenures_months=list(product.tenures_months),
        min_lock_in_months=product.min_lock_in_months,
        min_amount_minor=product.min_amount_minor,
        max_amount_minor=product.max_amount_minor,
        premature_closure_allowed=product.premature_penalty_bps is not None,
        premature_penalty_bps=product.premature_penalty_bps,
        bookable=product.bookable,
        investable=investable,
        notes=product.notes,
        catalogue_version=product.catalogue_version,
    )


def list_products(session: Session, risk_level: RiskLevel | None) -> list[WealthProductOut]:
    stmt = select(WealthProduct).order_by(WealthProduct.product_type, WealthProduct.code)
    if risk_level is not None:
        stmt = stmt.where(WealthProduct.risk_level == risk_level)
    investable = set(
        session.execute(
            select(InvestmentInstrument.wealth_product_id).where(
                InvestmentInstrument.wealth_product_id.is_not(None)
            )
        ).scalars()
    )
    return [_product_out(p, p.id in investable) for p in session.execute(stmt).scalars()]


def _insurance_out(product: InsuranceProduct) -> InsuranceProductOut:
    hints = insurance.eligible_age_bands(product.min_entry_age, product.max_entry_age)
    return InsuranceProductOut(
        id=product.id,
        code=product.code,
        insurance_type=product.insurance_type.value,
        name=product.name,
        description=product.description,
        currency="INR",
        cover_min_minor=product.min_cover_minor,
        cover_max_minor=product.max_cover_minor,
        policy_terms_years=list(product.policy_terms_years),
        indicative_premium=IndicativePremiumOut(
            annual_premium_minor=product.indicative_annual_premium_minor,
            reference_cover_minor=product.reference_cover_minor,
            reference_age=product.reference_age,
            reference_term_years=product.reference_term_years,
        ),
        eligibility=InsuranceEligibilityOut(
            min_entry_age=product.min_entry_age,
            max_entry_age=product.max_entry_age,
            max_cover_age=product.max_cover_age,
            age_bands=[
                AgeBandHintOut(age_band=h.age_band.value, whole_band_eligible=h.whole_band_eligible)
                for h in hints
            ],
        ),
        notes=product.notes,
        catalogue_version=product.catalogue_version,
    )


def list_insurance_products(
    session: Session, insurance_type: InsuranceType | None
) -> list[InsuranceProductOut]:
    """The seeded insurance catalogue (ADR-0028). Same answer for every customer: nothing about
    the caller is read, so no consent is needed and no underwriting happens."""
    stmt = select(InsuranceProduct).order_by(InsuranceProduct.insurance_type, InsuranceProduct.code)
    if insurance_type is not None:
        stmt = stmt.where(InsuranceProduct.insurance_type == insurance_type)
    return [_insurance_out(p) for p in session.execute(stmt).scalars()]


@dataclass(frozen=True, slots=True)
class _Terms:
    product: WealthProduct
    rate_bps: int
    figures: deposit_math.Maturity


def _bookable_terms(session: Session, request: DepositTermsRequest, start: date) -> _Terms:
    product = session.get(WealthProduct, request.product_id)
    if product is None:
        raise NotFoundError("wealth product not found")
    if (
        not product.bookable
        or product.product_type != ProductType.FIXED_DEPOSIT
        or product.annual_rate_bps is None
        or product.min_amount_minor is None
        or product.max_amount_minor is None
    ):
        raise ProductNotBookableError("this product cannot be booked online")
    if request.tenure_months not in product.tenures_months:
        raise UnprocessableError("tenure is not offered for this product")
    if not product.min_amount_minor <= request.amount_minor <= product.max_amount_minor:
        raise UnprocessableError("amount is outside this product's range")
    rate = product.annual_rate_bps
    figures = deposit_math.maturity(request.amount_minor, rate, request.tenure_months, start)
    return _Terms(product, rate, figures)


def quote(session: Session, request: DepositTermsRequest, now: datetime) -> DepositQuoteOut:
    """The exact figures booking would store, for the same terms on the same bank day."""
    start = bank_today(now)
    terms = _bookable_terms(session, request, start)
    return DepositQuoteOut(
        product_id=terms.product.id,
        product_name=terms.product.name,
        principal_minor=request.amount_minor,
        tenure_months=request.tenure_months,
        annual_rate_bps=terms.rate_bps,
        apy_bps=deposit_math.apy_bps(terms.rate_bps),
        start_date=start,
        maturity_date=terms.figures.maturity_date,
        maturity_interest_minor=terms.figures.interest_minor,
        maturity_amount_minor=terms.figures.maturity_amount_minor,
        lock_in_months=terms.product.min_lock_in_months,
        premature_penalty_bps=terms.product.premature_penalty_bps,
    )


# ------------------------------------------------------------------- holdings
def _lock_in_until(deposit: FixedDeposit) -> date:
    return deposit_math.add_months(deposit.start_date, deposit.lock_in_months)


def _accrued(deposit: FixedDeposit, today: date) -> int:
    return deposit_math.accrued_interest(
        deposit.principal, deposit.annual_rate_bps, deposit.start_date, deposit.maturity_date, today
    )


def _closure_interest(deposit: FixedDeposit, today: date) -> tuple[ClosureKind, int] | None:
    """What closing on ``today`` would pay as interest, or None if the deposit cannot be closed."""
    if deposit.status == FixedDepositStatus.CLOSED:
        return None
    if today >= deposit.maturity_date:
        return ClosureKind.MATURED, deposit.maturity_interest
    if deposit.premature_penalty_bps is None or today < _lock_in_until(deposit):
        return None
    interest = deposit_math.premature_interest(
        deposit.principal,
        deposit.annual_rate_bps,
        deposit.premature_penalty_bps,
        deposit.start_date,
        deposit.maturity_date,
        today,
    )
    return ClosureKind.PREMATURE, interest


def _status(deposit: FixedDeposit, today: date) -> str:
    if deposit.status == FixedDepositStatus.CLOSED:
        return "CLOSED"
    return "MATURED" if today >= deposit.maturity_date else "ACTIVE"


def _deposit_out(deposit: FixedDeposit, product_name: str, today: date) -> FixedDepositOut:
    closed = deposit.status == FixedDepositStatus.CLOSED
    closure = _closure_interest(deposit, today)
    return FixedDepositOut(
        id=deposit.id,
        product_id=deposit.product_id,
        product_name=product_name,
        status=_status(deposit, today),
        account_id=deposit.account_id,
        currency=deposit.currency,
        principal_minor=deposit.principal,
        annual_rate_bps=deposit.annual_rate_bps,
        tenure_months=deposit.tenure_months,
        start_date=deposit.start_date,
        maturity_date=deposit.maturity_date,
        days_to_maturity=0 if closed else max((deposit.maturity_date - today).days, 0),
        maturity_interest_minor=deposit.maturity_interest,
        maturity_amount_minor=deposit.maturity_amount,
        accrued_interest_minor=(deposit.paid_interest or 0) if closed else _accrued(deposit, today),
        lock_in_until=_lock_in_until(deposit),
        can_close_now=closure is not None,
        payout_if_closed_today_minor=None if closure is None else deposit.principal + closure[1],
        closure_kind=deposit.closure_kind.value if deposit.closure_kind else None,
        paid_interest_minor=deposit.paid_interest,
        closed_on=deposit.closed_on,
        booking_transaction_id=deposit.booking_transaction_id,
        closure_transaction_id=deposit.closure_transaction_id,
        interest_transaction_id=deposit.interest_transaction_id,
        booked_at=deposit.created_at,
    )


def _product_names(session: Session, ids: set[UUID]) -> dict[UUID, str]:
    if not ids:
        return {}
    rows = session.execute(
        select(WealthProduct.id, WealthProduct.name).where(WealthProduct.id.in_(ids))
    ).all()
    return {row.id: row.name for row in rows}


def list_fixed_deposits(
    session: Session, customer_id: UUID, now: datetime
) -> list[FixedDepositOut]:
    deposits = list_owned_fixed_deposits(session, customer_id)
    names = _product_names(session, {d.product_id for d in deposits})
    today = bank_today(now)
    return [_deposit_out(d, names[d.product_id], today) for d in deposits]


def get_fixed_deposit(
    session: Session, customer_id: UUID, deposit_id: UUID, now: datetime
) -> FixedDepositOut:
    deposit = get_owned_fixed_deposit(session, customer_id, deposit_id)
    names = _product_names(session, {deposit.product_id})
    return _deposit_out(deposit, names[deposit.product_id], bank_today(now))


def _allocation(values: dict[str, int]) -> list[AllocationSlice]:
    """Shares in basis points that sum to exactly 10000 (largest remainder), 0 when empty."""
    total = sum(values.values())
    if total <= 0:
        return [
            AllocationSlice(asset_class=k, value_minor=v, share_bps=0) for k, v in values.items()
        ]
    floors = {k: v * FULL_SHARE_BPS // total for k, v in values.items()}
    left = FULL_SHARE_BPS - sum(floors.values())
    by_remainder = sorted(values, key=lambda k: (-(values[k] * FULL_SHARE_BPS % total), k))
    for key in by_remainder[:left]:
        floors[key] += 1
    return [
        AllocationSlice(asset_class=k, value_minor=v, share_bps=floors[k])
        for k, v in values.items()
    ]


def _investments_by_kind(
    session: Session, customer_id: UUID, today: date
) -> tuple[dict[InstrumentKind, int], int]:
    """Value of the customer's holdings by kind at today's simulated prices, and their cost."""
    holdings = list_owned_holdings(session, customer_id)
    by_kind = dict.fromkeys(InstrumentKind, 0)
    if not holdings:
        return by_kind, 0
    ids = {h.instrument_id for h in holdings}
    instruments = {
        i.id: i
        for i in session.execute(
            select(InvestmentInstrument).where(InvestmentInstrument.id.in_(ids))
        ).scalars()
    }
    for holding in holdings:
        instrument = instruments[holding.instrument_id]
        price = pricing.instrument_price(instrument, today)
        by_kind[instrument.kind] += pricing.value_of_units(holding.units_micro, price)
    return by_kind, sum(h.cost_basis_minor for h in holdings)


def get_portfolio(session: Session, customer_id: UUID, now: datetime) -> PortfolioOut:
    today = bank_today(now)
    accounts = accounts_service.list_accounts(session, customer_id)
    cash = sum(a.ledger_balance for a in accounts if a.currency == CURRENCY)
    open_deposits = [
        d
        for d in list_owned_fixed_deposits(session, customer_id)
        if d.status == FixedDepositStatus.ACTIVE and d.currency == CURRENCY
    ]
    principal = sum(d.principal for d in open_deposits)
    accrued = sum(_accrued(d, today) for d in open_deposits)
    deposits_value = principal + accrued
    by_kind, invested_cost = _investments_by_kind(session, customer_id, today)
    funds = by_kind[InstrumentKind.MUTUAL_FUND]
    stocks = by_kind[InstrumentKind.STOCK]
    bonds = by_kind[InstrumentKind.BOND]
    return PortfolioOut(
        currency=CURRENCY,
        as_of=today,
        cash_minor=cash,
        invested_principal_minor=principal,
        accrued_interest_minor=accrued,
        deposits_value_minor=deposits_value,
        projected_maturity_value_minor=sum(d.maturity_amount for d in open_deposits),
        funds_minor=funds,
        stocks_minor=stocks,
        bonds_minor=bonds,
        investments_cost_minor=invested_cost,
        investments_gain_minor=funds + stocks + bonds - invested_cost,
        total_minor=cash + deposits_value + funds + stocks + bonds,
        open_deposits=len(open_deposits),
        allocation=_allocation(
            {
                "CASH": max(cash, 0),
                "DEPOSITS": deposits_value,
                "FUNDS": funds,
                "STOCKS": stocks,
                "BONDS": bonds,
            }
        ),
    )


# ------------------------------------------------------------ money movement
@dataclass(frozen=True, slots=True)
class WealthResult:
    status_code: int
    body: dict[str, Any]
    replayed: bool = False


def book_fixed_deposit(
    session: Session,
    settings: Settings,
    *,
    customer_id: UUID,
    user_id: UUID,
    request: DepositBookRequest,
    idempotency_key: str,
    meta: RequestMeta,
    now: datetime,
) -> WealthResult:
    """Book a deposit: one posting, the deposit row, audit, outbox, one commit (ADR-0025 section 4).
    Insufficient funds raises from the posting and leaves nothing behind (the caller rolls back)."""
    _lock_user(session, user_id)
    request_hash = _hash({"op": "wealth.fd.book"} | request.model_dump(mode="json"), user_id)
    idem_id, replay = _begin_idempotent(session, settings, user_id, idempotency_key, request_hash)
    if replay is not None:
        return replay

    account = get_owned_account(session, customer_id, request.account_id)
    if account.status != AccountStatus.ACTIVE:
        raise UnprocessableError("the funding account must be active")
    if account.currency != CURRENCY:
        raise UnprocessableError("fixed deposits are booked from INR accounts only")
    start = bank_today(now)
    terms = _bookable_terms(session, request, start)
    booking = ledger.book_fixed_deposit(
        session,
        account_id=account.id,
        amount=request.amount_minor,
        currency=CURRENCY,
        initiated_by_user_id=user_id,
        reference=f"{terms.product.name} booked",
    )
    deposit = FixedDeposit(
        customer_id=customer_id,
        user_id=user_id,
        account_id=account.id,
        product_id=terms.product.id,
        booking_transaction_id=booking.id,
        annual_rate_bps=terms.rate_bps,
        tenure_months=request.tenure_months,
        lock_in_months=terms.product.min_lock_in_months,
        premature_penalty_bps=terms.product.premature_penalty_bps,
        principal=request.amount_minor,
        maturity_interest=terms.figures.interest_minor,
        maturity_amount=terms.figures.maturity_amount_minor,
        currency=CURRENCY,
        start_date=start,
        maturity_date=terms.figures.maturity_date,
        status=FixedDepositStatus.ACTIVE,
    )
    session.add(deposit)
    session.flush()
    session.refresh(deposit)  # server-side created_at for the response
    notifications.enqueue(
        session,
        notifications.FIXED_DEPOSIT_BOOKED,
        {
            "user_id": str(user_id),
            "transaction_id": str(booking.id),
            "account_id": str(account.id),
            "amount_minor": deposit.principal,
            "currency": CURRENCY,
            "maturity_date": deposit.maturity_date.isoformat(),
            "maturity_amount_minor": deposit.maturity_amount,
        },
    )
    AuditService(session).record(
        user_id,
        "FIXED_DEPOSIT_BOOKED",
        "fixed_deposit",
        str(deposit.id),
        ip=meta.ip,
        request_id=meta.request_id,
        details={
            "product_id": str(terms.product.id),
            "amount_minor": deposit.principal,
            "tenure_months": deposit.tenure_months,
            "annual_rate_bps": deposit.annual_rate_bps,
            "maturity_date": deposit.maturity_date.isoformat(),
            "transaction_id": str(booking.id),
        },
    )
    outcome = WealthResult(
        201, _deposit_out(deposit, terms.product.name, start).model_dump(mode="json")
    )
    _store_response(session, idem_id, outcome, booking.id)
    session.commit()
    return outcome


def close_fixed_deposit(
    session: Session,
    settings: Settings,
    *,
    customer_id: UUID,
    user_id: UUID,
    deposit_id: UUID,
    idempotency_key: str,
    meta: RequestMeta,
    now: datetime,
) -> WealthResult:
    """Pay a deposit out: at maturity the projected amount, before it the penalty rate for the
    time held (ADR-0025 sections 2 and 5). The deposit row lock makes the payout exactly-once."""
    _lock_user(session, user_id)
    request_hash = _hash({"op": "wealth.fd.close", "deposit_id": str(deposit_id)}, user_id)
    idem_id, replay = _begin_idempotent(session, settings, user_id, idempotency_key, request_hash)
    if replay is not None:
        return replay

    deposit = get_owned_fixed_deposit(session, customer_id, deposit_id, for_update=True)
    if deposit.status == FixedDepositStatus.CLOSED:
        raise DepositClosedError("this fixed deposit is already closed")
    today = bank_today(now)
    closure = _closure_interest(deposit, today)
    if closure is None:
        raise DepositLockedInError(
            f"this fixed deposit cannot be closed before {_lock_in_until(deposit).isoformat()}"
        )
    kind, interest = closure
    name = _product_names(session, {deposit.product_id})[deposit.product_id]
    payout = ledger.pay_out_fixed_deposit(
        session,
        account_id=deposit.account_id,
        principal=deposit.principal,
        interest=interest,
        currency=deposit.currency,
        initiated_by_user_id=user_id,
        reference=f"{name} closed",
    )
    deposit.status = FixedDepositStatus.CLOSED
    deposit.closure_kind = kind
    deposit.paid_interest = interest
    deposit.closed_on = today
    deposit.closed_at = utcnow()
    deposit.closure_transaction_id = payout.principal.id
    deposit.interest_transaction_id = payout.interest.id if payout.interest else None
    session.flush()
    notifications.enqueue(
        session,
        notifications.FIXED_DEPOSIT_CLOSED,
        {
            "user_id": str(user_id),
            "transaction_id": str(payout.principal.id),
            "account_id": str(deposit.account_id),
            "amount_minor": deposit.principal + interest,
            "interest_minor": interest,
            "currency": deposit.currency,
        },
    )
    AuditService(session).record(
        user_id,
        "FIXED_DEPOSIT_CLOSED",
        "fixed_deposit",
        str(deposit.id),
        ip=meta.ip,
        request_id=meta.request_id,
        details={
            "closure_kind": kind.value,
            "principal_minor": deposit.principal,
            "interest_minor": interest,
            "transaction_id": str(payout.principal.id),
            "interest_transaction_id": str(payout.interest.id) if payout.interest else None,
        },
    )
    outcome = WealthResult(200, _deposit_out(deposit, name, today).model_dump(mode="json"))
    _store_response(session, idem_id, outcome, payout.principal.id)
    session.commit()
    return outcome


def _lock_user(session: Session, user_id: UUID) -> None:
    """The same per-user advisory lock as transfers and loans: ALWAYS the first lock."""
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:u))"), {"u": str(user_id)})


def _hash(fields: dict[str, Any], user_id: UUID) -> str:
    """Includes an "op" field, so a key reused across endpoints is a reuse error, never a replay
    of another endpoint's response."""
    canonical = json.dumps(fields | {"user": str(user_id)}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _begin_idempotent(
    session: Session, settings: Settings, user_id: UUID, key: str, request_hash: str
) -> tuple[UUID, WealthResult | None]:
    """Claim the key. A concurrent duplicate is already serialised by the user lock; it then sees
    the committed row and replays its response."""
    ledger.discard_expired_idempotency_key(session, user_id, key)
    inserted = session.execute(
        pg_insert(IdempotencyKey)
        .values(
            id=uuid4(),
            user_id=user_id,
            key=key,
            request_hash=request_hash,
            expires_at=utcnow() + timedelta(hours=settings.idempotency_ttl_hours),
        )
        .on_conflict_do_nothing(constraint="uq_idempotency_keys_user_key")
        .returning(IdempotencyKey.id)
    ).scalar_one_or_none()
    if inserted is not None:
        return inserted, None
    existing = session.execute(
        select(IdempotencyKey).where(IdempotencyKey.user_id == user_id, IdempotencyKey.key == key)
    ).scalar_one()
    if existing.request_hash != request_hash:
        raise IdempotencyKeyReuseError("Idempotency-Key was already used with a different request")
    if existing.response_status is None or existing.response_body is None:
        raise ConflictError("a request with this Idempotency-Key is still in progress")
    IDEMPOTENT_REPLAYS_TOTAL.inc()
    return existing.id, WealthResult(existing.response_status, existing.response_body, True)


def _store_response(
    session: Session, idem_id: UUID, outcome: WealthResult, transaction_id: UUID | None
) -> None:
    """``transaction_id`` is None for a use case that posts nothing (an insurance application)."""
    row = session.get(IdempotencyKey, idem_id)
    if row is None:
        raise RuntimeError("idempotency row vanished")
    row.response_status = outcome.status_code
    row.response_body = outcome.body
    row.transaction_id = transaction_id
