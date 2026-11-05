"""Simulated investing in funds, stocks and bonds through the ledger (ADR-0058).

A purchase or a sale is ONE use case and ONE database transaction, in the AGENTS.md lock order:
the per-user advisory lock, the idempotency key, the holding row ``FOR UPDATE`` (a purchase first
inserts a zero holding if there is none), then the accounts ``FOR UPDATE`` in sorted id order
inside the ledger BEFORE the transaction header and the order row. Prices come only from
``wealth/pricing.py`` (simulated, one price per bank day); no price, cost or customer is taken from
the client. Insufficient funds or units raise before the commit and leave nothing behind.
"""

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from pbip_api.core.calendar import bank_today
from pbip_api.core.config import Settings
from pbip_api.core.db import utcnow
from pbip_api.core.errors import NotFoundError, UnprocessableError
from pbip_api.modules.accounts.models import Account, AccountStatus
from pbip_api.modules.accounts.policy import get_owned_account
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.notifications import service as notifications
from pbip_api.modules.wealth import pricing
from pbip_api.modules.wealth.models import (
    InstrumentKind,
    InvestmentHolding,
    InvestmentInstrument,
    InvestmentOrder,
    OrderSide,
)
from pbip_api.modules.wealth.policy import get_owned_holding, list_owned_holdings
from pbip_api.modules.wealth.schemas import (
    BuyRequest,
    HoldingOut,
    InstrumentOut,
    OrderOut,
    SellRequest,
)
from pbip_api.modules.wealth.service import (
    CURRENCY,
    WealthResult,
    _begin_idempotent,
    _hash,
    _lock_user,
    _store_response,
)


class InsufficientUnitsError(UnprocessableError):
    code = "insufficient_units"


class OrderTooSmallError(UnprocessableError):
    code = "order_too_small"


# ------------------------------------------------------------------------------ reads
def price_today(instrument: InvestmentInstrument, today: date) -> int:
    return pricing.instrument_price(instrument, today)


def _by_units(instrument: InvestmentInstrument) -> bool:
    return instrument.kind != InstrumentKind.MUTUAL_FUND


def _instrument_out(instrument: InvestmentInstrument, today: date) -> InstrumentOut:
    return InstrumentOut(
        id=instrument.id,
        code=instrument.code,
        kind=instrument.kind.value,
        name=instrument.name,
        description=instrument.description,
        risk_level=instrument.risk_level.value,
        wealth_product_id=instrument.wealth_product_id,
        currency="INR",
        order_mode="UNITS" if _by_units(instrument) else "AMOUNT",
        min_order_minor=instrument.min_order_minor,
        price_minor=price_today(instrument, today),
        price_date=today,
        price_source="SIMULATED",
        notes=instrument.notes,
        catalogue_version=instrument.catalogue_version,
    )


def list_instruments(session: Session, now: datetime) -> list[InstrumentOut]:
    """The investable instruments with today's simulated price. Same for every customer."""
    today = bank_today(now)
    stmt = select(InvestmentInstrument).order_by(
        InvestmentInstrument.kind, InvestmentInstrument.code
    )
    return [_instrument_out(i, today) for i in session.execute(stmt).scalars()]


def _instruments(session: Session, ids: set[UUID]) -> dict[UUID, InvestmentInstrument]:
    if not ids:
        return {}
    stmt = select(InvestmentInstrument).where(InvestmentInstrument.id.in_(ids))
    return {i.id: i for i in session.execute(stmt).scalars()}


def _holding_out(
    holding: InvestmentHolding, instrument: InvestmentInstrument, today: date
) -> HoldingOut:
    price = price_today(instrument, today)
    value = pricing.value_of_units(holding.units_micro, price)
    return HoldingOut(
        id=holding.id,
        instrument_id=instrument.id,
        instrument_code=instrument.code,
        instrument_name=instrument.name,
        kind=instrument.kind.value,
        risk_level=instrument.risk_level.value,
        units_micro=holding.units_micro,
        price_minor=price,
        price_date=today,
        price_source="SIMULATED",
        value_minor=value,
        cost_basis_minor=holding.cost_basis_minor,
        gain_minor=value - holding.cost_basis_minor,
        gain_bps=pricing.gain_bps(value, holding.cost_basis_minor),
    )


def list_holdings(session: Session, customer_id: UUID, now: datetime) -> list[HoldingOut]:
    holdings = list_owned_holdings(session, customer_id)
    instruments = _instruments(session, {h.instrument_id for h in holdings})
    today = bank_today(now)
    return [_holding_out(h, instruments[h.instrument_id], today) for h in holdings]


def get_holding(session: Session, customer_id: UUID, holding_id: UUID, now: datetime) -> HoldingOut:
    holding = get_owned_holding(session, customer_id, holding_id)
    instrument = _instruments(session, {holding.instrument_id})[holding.instrument_id]
    return _holding_out(holding, instrument, bank_today(now))


def _order_out(order: InvestmentOrder, instrument: InvestmentInstrument) -> OrderOut:
    return OrderOut(
        id=order.id,
        side=order.side.value,
        holding_id=order.holding_id,
        instrument_id=instrument.id,
        instrument_code=instrument.code,
        instrument_name=instrument.name,
        account_id=order.account_id,
        units_micro=order.units_micro,
        price_minor=order.price_minor,
        price_date=order.price_date,
        price_source="SIMULATED",
        amount_minor=order.amount_minor,
        cost_basis_minor=order.cost_basis_minor,
        realised_gain_minor=order.realised_gain_minor,
        transaction_id=order.transaction_id,
        sip_id=order.sip_id,
        created_at=order.created_at,
    )


# ----------------------------------------------------------------------- money movement
@dataclass(frozen=True, slots=True)
class Purchase:
    """What a purchase debits and adds, fixed before any lock is taken."""

    instrument: InvestmentInstrument
    price_minor: int
    units_micro: int
    amount_minor: int


def price_purchase(
    instrument: InvestmentInstrument,
    today: date,
    *,
    amount_minor: int | None,
    units: int | None,
) -> Purchase:
    """Validate the quantity against the instrument and fix the day's price, units and amount."""
    price = price_today(instrument, today)
    if _by_units(instrument):
        if units is None or amount_minor is not None:
            raise UnprocessableError("stocks and bonds are bought in whole units")
        return Purchase(instrument, price, units * pricing.MICRO_PER_UNIT, units * price)
    if amount_minor is None or units is not None:
        raise UnprocessableError("funds are bought by amount")
    if instrument.min_order_minor is not None and amount_minor < instrument.min_order_minor:
        raise OrderTooSmallError("the amount is below this fund's minimum purchase")
    units_micro = pricing.units_for_amount(amount_minor, price)
    if units_micro <= 0:
        raise OrderTooSmallError("the amount buys no units at today's price")
    return Purchase(instrument, price, units_micro, amount_minor)


def get_instrument(session: Session, instrument_id: UUID) -> InvestmentInstrument:
    instrument = session.get(InvestmentInstrument, instrument_id)
    if instrument is None:
        raise NotFoundError("instrument not found")
    return instrument


def _require_funding_account(account: Account) -> None:
    if account.status != AccountStatus.ACTIVE:
        raise UnprocessableError("the account must be active")
    if account.currency != CURRENCY:
        raise UnprocessableError("investments are paid from and to INR accounts only")


def _locked_holding_for_purchase(
    session: Session, customer_id: UUID, instrument_id: UUID
) -> InvestmentHolding:
    """Insert a zero holding if there is none, then lock it (both under the user lock)."""
    session.execute(
        pg_insert(InvestmentHolding)
        .values(customer_id=customer_id, instrument_id=instrument_id, units_micro=0,
                cost_basis_minor=0)
        .on_conflict_do_nothing(constraint="uq_investment_holdings_customer_id_instrument_id")
    )  # fmt: skip
    return session.execute(
        select(InvestmentHolding)
        .where(
            InvestmentHolding.customer_id == customer_id,
            InvestmentHolding.instrument_id == instrument_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()


def execute_purchase(
    session: Session,
    *,
    customer_id: UUID,
    user_id: UUID,
    account: Account,
    purchase: Purchase,
    today: date,
    channel: str = "WEB",
    sip_id: UUID | None = None,
) -> InvestmentOrder:
    """Steps 3-5 of ADR-0058 section 6, shared by a customer order and a SIP instalment. The
    caller holds the user lock and the idempotency key, and owns the commit."""
    _require_funding_account(account)
    instrument = purchase.instrument
    holding = _locked_holding_for_purchase(session, customer_id, instrument.id)
    payment = ledger.buy_investment(
        session,
        account_id=account.id,
        amount=purchase.amount_minor,
        currency=CURRENCY,
        initiated_by_user_id=user_id,
        reference=f"Bought {instrument.name}"[:140],
        channel=channel,
    )
    holding.units_micro += purchase.units_micro
    holding.cost_basis_minor += purchase.amount_minor
    holding.updated_at = utcnow()
    order = InvestmentOrder(
        customer_id=customer_id,
        user_id=user_id,
        holding_id=holding.id,
        instrument_id=instrument.id,
        account_id=account.id,
        transaction_id=payment.id,
        sip_id=sip_id,
        side=OrderSide.BUY,
        units_micro=purchase.units_micro,
        price_minor=purchase.price_minor,
        price_date=today,
        amount_minor=purchase.amount_minor,
        cost_basis_minor=purchase.amount_minor,
        realised_gain_minor=None,
    )
    session.add(order)
    session.flush()
    session.refresh(order)  # server-side created_at for the response
    return order


def _notify(session: Session, event_type: str, user_id: UUID, order: InvestmentOrder) -> None:
    notifications.enqueue(
        session,
        event_type,
        {
            "user_id": str(user_id),
            "transaction_id": str(order.transaction_id),
            "account_id": str(order.account_id),
            "amount_minor": order.amount_minor,
            "currency": CURRENCY,
        },
    )


def _audit_details(order: InvestmentOrder) -> dict[str, object]:
    return {
        "instrument_id": str(order.instrument_id),
        "holding_id": str(order.holding_id),
        "units_micro": order.units_micro,
        "price_minor": order.price_minor,
        "price_date": order.price_date.isoformat(),
        "amount_minor": order.amount_minor,
        "transaction_id": str(order.transaction_id),
        "price_source": pricing.PRICE_SOURCE,
    }


def buy(
    session: Session,
    settings: Settings,
    *,
    customer_id: UUID,
    user_id: UUID,
    request: BuyRequest,
    idempotency_key: str,
    meta: RequestMeta,
    now: datetime,
) -> WealthResult:
    """Buy fund units by amount, or whole stocks or bonds (ADR-0058 section 6)."""
    _lock_user(session, user_id)
    request_hash = _hash({"op": "wealth.investment.buy"} | request.model_dump(mode="json"), user_id)
    idem_id, replay = _begin_idempotent(session, settings, user_id, idempotency_key, request_hash)
    if replay is not None:
        return replay

    today = bank_today(now)
    instrument = get_instrument(session, request.instrument_id)
    purchase = price_purchase(
        instrument, today, amount_minor=request.amount_minor, units=request.units
    )
    account = get_owned_account(session, customer_id, request.account_id)
    order = execute_purchase(
        session,
        customer_id=customer_id,
        user_id=user_id,
        account=account,
        purchase=purchase,
        today=today,
    )
    _notify(session, notifications.INVESTMENT_BOUGHT, user_id, order)
    AuditService(session).record(
        user_id,
        "INVESTMENT_BOUGHT",
        "investment_order",
        str(order.id),
        ip=meta.ip,
        request_id=meta.request_id,
        details=_audit_details(order),
    )
    outcome = WealthResult(201, _order_out(order, instrument).model_dump(mode="json"))
    _store_response(session, idem_id, outcome, order.transaction_id)
    session.commit()
    return outcome


def _units_to_sell(instrument: InvestmentInstrument, holding: InvestmentHolding, units: int) -> int:
    if _by_units(instrument) and units % pricing.MICRO_PER_UNIT != 0:
        raise UnprocessableError("stocks and bonds are sold in whole units")
    if units > holding.units_micro:
        raise InsufficientUnitsError("you hold fewer units than you asked to sell")
    return units


def sell(
    session: Session,
    settings: Settings,
    *,
    customer_id: UUID,
    user_id: UUID,
    holding_id: UUID,
    request: SellRequest,
    idempotency_key: str,
    meta: RequestMeta,
    now: datetime,
) -> WealthResult:
    """Sell units of one of my holdings into one of my accounts (ADR-0058 section 6)."""
    _lock_user(session, user_id)
    request_hash = _hash(
        {"op": "wealth.investment.sell", "holding_id": str(holding_id)}
        | request.model_dump(mode="json"),
        user_id,
    )
    idem_id, replay = _begin_idempotent(session, settings, user_id, idempotency_key, request_hash)
    if replay is not None:
        return replay

    holding = get_owned_holding(session, customer_id, holding_id, for_update=True)
    instrument = get_instrument(session, holding.instrument_id)
    units = _units_to_sell(instrument, holding, request.units_micro)
    today = bank_today(now)
    price = price_today(instrument, today)
    proceeds = pricing.value_of_units(units, price)
    if proceeds <= 0:
        raise OrderTooSmallError("these units are worth less than one paisa at today's price")
    account = get_owned_account(session, customer_id, request.account_id)
    _require_funding_account(account)
    cost = pricing.cost_removed(holding.cost_basis_minor, holding.units_micro, units)
    payout = ledger.sell_investment(
        session,
        account_id=account.id,
        amount=proceeds,
        currency=CURRENCY,
        initiated_by_user_id=user_id,
        reference=f"Sold {instrument.name}"[:140],
    )
    holding.units_micro -= units
    holding.cost_basis_minor -= cost
    holding.updated_at = utcnow()
    order = InvestmentOrder(
        customer_id=customer_id,
        user_id=user_id,
        holding_id=holding.id,
        instrument_id=instrument.id,
        account_id=account.id,
        transaction_id=payout.id,
        side=OrderSide.SELL,
        units_micro=units,
        price_minor=price,
        price_date=today,
        amount_minor=proceeds,
        cost_basis_minor=cost,
        realised_gain_minor=proceeds - cost,
    )
    session.add(order)
    session.flush()
    session.refresh(order)
    _notify(session, notifications.INVESTMENT_SOLD, user_id, order)
    AuditService(session).record(
        user_id,
        "INVESTMENT_SOLD",
        "investment_order",
        str(order.id),
        ip=meta.ip,
        request_id=meta.request_id,
        details=_audit_details(order) | {"realised_gain_minor": order.realised_gain_minor},
    )
    outcome = WealthResult(200, _order_out(order, instrument).model_dump(mode="json"))
    _store_response(session, idem_id, outcome, payout.id)
    session.commit()
    return outcome
