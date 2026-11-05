"""Wealth HTTP routes (TASK-063, TASK-064). Customer-only; nothing names another customer: the
customer is always the authenticated one, and deposits are read via ``wealth/policy``."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import get_session
from pbip_api.modules.auth.dependencies import (
    Principal,
    get_clock,
    get_current_principal,
    get_request_meta,
)
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.customers.dependencies import get_current_customer
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.wealth import (
    insurance_service,
    investing_service,
    service,
    sip_service,
    suitability_service,
)
from pbip_api.modules.wealth.models import InsuranceType, RiskLevel
from pbip_api.modules.wealth.schemas import (
    BuyRequest,
    DepositBookRequest,
    DepositQuoteOut,
    DepositTermsRequest,
    FixedDepositOut,
    HoldingOut,
    InstrumentOut,
    InsuranceApplicationOut,
    InsuranceProductOut,
    InsuranceQuoteOut,
    InsuranceQuoteRequest,
    OrderOut,
    PortfolioOut,
    SellRequest,
    SipCreateRequest,
    SipOut,
    SuitabilityOut,
    WealthProductOut,
)

IdempotencyKeyHeader = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description="Unique per logical request; retrying with the same key never duplicates.",
    ),
]

router = APIRouter(prefix="/api/v1/wealth", tags=["wealth"])


def _now(clock: Callable[[], float]) -> datetime:
    return datetime.fromtimestamp(clock(), tz=UTC)


def _replayable(result: service.WealthResult) -> JSONResponse:
    headers = {"Idempotent-Replayed": "true"} if result.replayed else None
    return JSONResponse(result.body, status_code=result.status_code, headers=headers)


@router.get(
    "/products",
    response_model=list[WealthProductOut],
    summary="Wealth catalogue: fixed deposits, mutual funds and the sovereign gold bond",
)
def list_products(
    risk_level: RiskLevel | None = Query(default=None, description="Only this risk level"),
    _customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
) -> list[WealthProductOut]:
    return service.list_products(session, risk_level)


@router.get(
    "/insurance",
    response_model=list[InsuranceProductOut],
    summary="Insurance catalogue: term life and health, with indicative premiums (no underwriting)",
)
def list_insurance(
    insurance_type: InsuranceType | None = Query(default=None, description="Only this type"),
    _customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
) -> list[InsuranceProductOut]:
    return service.list_insurance_products(session, insurance_type)


@router.post(
    "/fixed-deposits/quote",
    response_model=DepositQuoteOut,
    summary="Exact maturity preview for a fixed deposit (books nothing)",
    responses={404: {"description": "No such wealth product (problem+json)"}},
)
def quote_fixed_deposit(
    body: DepositTermsRequest,
    _customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
    clock: Callable[[], float] = Depends(get_clock),
) -> DepositQuoteOut:
    return service.quote(session, body, _now(clock))


@router.post(
    "/fixed-deposits/book",
    response_model=FixedDepositOut,
    status_code=201,
    summary="Book a fixed deposit from one of my accounts (idempotent)",
    responses={
        404: {"description": "No such wealth product, or the account is not mine (problem+json)"},
        422: {"description": "Insufficient funds, terms not offered or invalid request"},
    },
)
def book_fixed_deposit(
    body: DepositBookRequest,
    idempotency_key: IdempotencyKeyHeader,
    customer: Customer = Depends(get_current_customer),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    meta: RequestMeta = Depends(get_request_meta),
    clock: Callable[[], float] = Depends(get_clock),
) -> JSONResponse:
    return _replayable(
        service.book_fixed_deposit(
            session,
            settings,
            customer_id=customer.id,
            user_id=principal.user_id,
            request=body,
            idempotency_key=idempotency_key,
            meta=meta,
            now=_now(clock),
        )
    )


@router.get("/fixed-deposits", response_model=list[FixedDepositOut], summary="My fixed deposits")
def list_fixed_deposits(
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
    clock: Callable[[], float] = Depends(get_clock),
) -> list[FixedDepositOut]:
    return service.list_fixed_deposits(session, customer.id, _now(clock))


@router.get(
    "/fixed-deposits/{deposit_id}",
    response_model=FixedDepositOut,
    summary="One of my fixed deposits",
)
def get_fixed_deposit(
    deposit_id: UUID,
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
    clock: Callable[[], float] = Depends(get_clock),
) -> FixedDepositOut:
    return service.get_fixed_deposit(session, customer.id, deposit_id, _now(clock))


@router.post(
    "/fixed-deposits/{deposit_id}/close",
    response_model=FixedDepositOut,
    summary="Close one of my fixed deposits: maturity payout, or premature with the penalty rate",
    responses={
        409: {"description": "Already closed"},
        422: {"description": "Still inside its lock-in period"},
    },
)
def close_fixed_deposit(
    deposit_id: UUID,
    idempotency_key: IdempotencyKeyHeader,
    customer: Customer = Depends(get_current_customer),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    meta: RequestMeta = Depends(get_request_meta),
    clock: Callable[[], float] = Depends(get_clock),
) -> JSONResponse:
    return _replayable(
        service.close_fixed_deposit(
            session,
            settings,
            customer_id=customer.id,
            user_id=principal.user_id,
            deposit_id=deposit_id,
            idempotency_key=idempotency_key,
            meta=meta,
            now=_now(clock),
        )
    )


@router.get(
    "/portfolio",
    response_model=PortfolioOut,
    summary="My portfolio: cash, deposits with accrued interest, and allocation",
)
def get_portfolio(
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
    clock: Callable[[], float] = Depends(get_clock),
) -> PortfolioOut:
    return service.get_portfolio(session, customer.id, _now(clock))


# ------------------------------------------------------------------ investing (ADR-0058)
@router.get(
    "/instruments",
    response_model=list[InstrumentOut],
    summary="Investable funds, stocks and bonds with today's SIMULATED price (not market data)",
)
def list_instruments(
    _customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
    clock: Callable[[], float] = Depends(get_clock),
) -> list[InstrumentOut]:
    return investing_service.list_instruments(session, _now(clock))


@router.post(
    "/investments/buy",
    response_model=OrderOut,
    status_code=201,
    summary="Buy fund units by amount, or whole stocks or bonds, from one of my accounts",
    responses={
        404: {"description": "No such instrument, or the account is not mine (problem+json)"},
        422: {"description": "Insufficient funds, wrong quantity kind or too small an order"},
    },
)
def buy_investment(
    body: BuyRequest,
    idempotency_key: IdempotencyKeyHeader,
    customer: Customer = Depends(get_current_customer),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    meta: RequestMeta = Depends(get_request_meta),
    clock: Callable[[], float] = Depends(get_clock),
) -> JSONResponse:
    return _replayable(
        investing_service.buy(
            session,
            settings,
            customer_id=customer.id,
            user_id=principal.user_id,
            request=body,
            idempotency_key=idempotency_key,
            meta=meta,
            now=_now(clock),
        )
    )


@router.get(
    "/holdings",
    response_model=list[HoldingOut],
    summary="My fund, stock and bond holdings at today's simulated price, with gain or loss",
)
def list_holdings(
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
    clock: Callable[[], float] = Depends(get_clock),
) -> list[HoldingOut]:
    return investing_service.list_holdings(session, customer.id, _now(clock))


@router.get("/holdings/{holding_id}", response_model=HoldingOut, summary="One of my holdings")
def get_holding(
    holding_id: UUID,
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
    clock: Callable[[], float] = Depends(get_clock),
) -> HoldingOut:
    return investing_service.get_holding(session, customer.id, holding_id, _now(clock))


@router.post(
    "/holdings/{holding_id}/sell",
    response_model=OrderOut,
    summary="Sell units of one of my holdings into one of my accounts (idempotent)",
    responses={
        404: {"description": "The holding or the account is not mine (problem+json)"},
        422: {"description": "More units than held, part of a stock or bond, or too small"},
    },
)
def sell_investment(
    holding_id: UUID,
    body: SellRequest,
    idempotency_key: IdempotencyKeyHeader,
    customer: Customer = Depends(get_current_customer),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    meta: RequestMeta = Depends(get_request_meta),
    clock: Callable[[], float] = Depends(get_clock),
) -> JSONResponse:
    return _replayable(
        investing_service.sell(
            session,
            settings,
            customer_id=customer.id,
            user_id=principal.user_id,
            holding_id=holding_id,
            request=body,
            idempotency_key=idempotency_key,
            meta=meta,
            now=_now(clock),
        )
    )


# ------------------------------------------------------------------ fund SIPs (ADR-0058 s. 7)
@router.post(
    "/sips",
    response_model=SipOut,
    status_code=201,
    summary="Set up a monthly fund purchase from one of my accounts (idempotent; moves no money)",
    responses={
        404: {"description": "No such instrument, or the account is not mine (problem+json)"},
        422: {"description": "Not a fund, below the fund's minimum, or invalid request"},
    },
)
def create_sip(
    body: SipCreateRequest,
    idempotency_key: IdempotencyKeyHeader,
    customer: Customer = Depends(get_current_customer),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    meta: RequestMeta = Depends(get_request_meta),
    clock: Callable[[], float] = Depends(get_clock),
) -> JSONResponse:
    return _replayable(
        sip_service.create_sip(
            session,
            settings,
            customer_id=customer.id,
            user_id=principal.user_id,
            request=body,
            idempotency_key=idempotency_key,
            meta=meta,
            now=_now(clock),
        )
    )


@router.get("/sips", response_model=list[SipOut], summary="My fund SIPs with their latest runs")
def list_sips(
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
) -> list[SipOut]:
    return sip_service.list_sips(session, customer.id)


@router.get("/sips/{sip_id}", response_model=SipOut, summary="One of my fund SIPs")
def get_sip(
    sip_id: UUID,
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
) -> SipOut:
    return sip_service.get_sip(session, customer.id, sip_id)


@router.post(
    "/sips/{sip_id}/cancel",
    response_model=SipOut,
    summary="Cancel one of my fund SIPs (no further instalments; safe to repeat)",
)
def cancel_sip(
    sip_id: UUID,
    customer: Customer = Depends(get_current_customer),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_session),
    meta: RequestMeta = Depends(get_request_meta),
) -> SipOut:
    return sip_service.cancel_sip(
        session, customer_id=customer.id, user_id=principal.user_id, sip_id=sip_id, meta=meta
    )


# ------------------------------------- insurance quote and application (ADR-0058 section 8)
@router.post(
    "/insurance/quote",
    response_model=InsuranceQuoteOut,
    summary="Simulated insurance quote from my declared age band (no underwriting; stores nothing)",
    responses={
        404: {"description": "No such insurance product (problem+json)"},
        422: {"description": "Cover or term not offered, or consent not given"},
    },
)
def quote_insurance(
    body: InsuranceQuoteRequest,
    _customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
) -> InsuranceQuoteOut:
    return insurance_service.quote(session, body)


@router.post(
    "/insurance/applications",
    response_model=InsuranceApplicationOut,
    status_code=201,
    summary="Apply for an insurance plan (simulated; no policy is issued and nothing is charged)",
    responses={
        404: {"description": "No such insurance product (problem+json)"},
        422: {"description": "Cover or term not offered, or consent not given"},
    },
)
def apply_for_insurance(
    body: InsuranceQuoteRequest,
    idempotency_key: IdempotencyKeyHeader,
    customer: Customer = Depends(get_current_customer),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    meta: RequestMeta = Depends(get_request_meta),
) -> JSONResponse:
    return _replayable(
        insurance_service.apply(
            session,
            settings,
            customer_id=customer.id,
            user_id=principal.user_id,
            request=body,
            idempotency_key=idempotency_key,
            meta=meta,
        )
    )


@router.get(
    "/insurance/applications",
    response_model=list[InsuranceApplicationOut],
    summary="My insurance applications with their status",
)
def list_insurance_applications(
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
) -> list[InsuranceApplicationOut]:
    return insurance_service.list_applications(session, customer.id)


@router.get(
    "/insurance/applications/{application_id}",
    response_model=InsuranceApplicationOut,
    summary="One of my insurance applications",
)
def get_insurance_application(
    application_id: UUID,
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
) -> InsuranceApplicationOut:
    return insurance_service.get_application(session, customer.id, application_id)


# ------------------------------------------------- suitability by risk band (ADR-0058 s. 9)
@router.get(
    "/suitability",
    response_model=SuitabilityOut,
    summary="Which wealth products suit my recorded risk band (RTQ-1), with the reason",
)
def get_suitability(
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
) -> SuitabilityOut:
    return suitability_service.suitability(session, customer.id)
