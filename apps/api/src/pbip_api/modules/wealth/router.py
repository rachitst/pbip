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
from pbip_api.modules.wealth import service
from pbip_api.modules.wealth.models import RiskLevel
from pbip_api.modules.wealth.schemas import (
    DepositBookRequest,
    DepositQuoteOut,
    DepositTermsRequest,
    FixedDepositOut,
    PortfolioOut,
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


@router.post(
    "/fixed-deposits/quote",
    response_model=DepositQuoteOut,
    summary="Exact maturity preview for a fixed deposit (books nothing)",
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
    responses={422: {"description": "Insufficient funds, terms not offered or invalid request"}},
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
