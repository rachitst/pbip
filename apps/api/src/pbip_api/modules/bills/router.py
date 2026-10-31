"""HTTP endpoints for listing utility billers and paying bills."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from pbip_ml.fraud.contract import FraudScorer
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import get_session
from pbip_api.core.geoip import CountryResolver
from pbip_api.modules.auth.dependencies import (
    Principal,
    get_current_principal,
    get_request_meta,
)
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.bills.models import Biller
from pbip_api.modules.bills.schemas import BillerOut, BillPaymentOut, PayBillRequest
from pbip_api.modules.bills.service import BillPaymentResult, BillsService, PayBillCommand
from pbip_api.modules.customers.dependencies import get_current_customer
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.fraud.dependencies import get_country_resolver, get_fraud_scorer

router = APIRouter(prefix="/api/v1/bills", tags=["bills"])

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


def get_bills_service(
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    scorer: FraudScorer = Depends(get_fraud_scorer),
    countries: CountryResolver = Depends(get_country_resolver),
) -> BillsService:
    return BillsService(session, settings, scorer, countries)


def _respond(result: BillPaymentResult) -> JSONResponse:
    headers = {"Idempotent-Replayed": "true"} if result.replayed else None
    return JSONResponse(result.body, status_code=result.status_code, headers=headers)


@router.get(
    "/billers",
    response_model=list[BillerOut],
    summary="List registered utility billers",
)
def list_billers(
    active_only: bool = Query(default=True, description="Filter to active billers only"),
    _principal: Principal = Depends(get_current_principal),
    service: BillsService = Depends(get_bills_service),
) -> list[Biller]:
    return service.list_billers(active_only=active_only)


@router.post(
    "/pay",
    response_model=BillPaymentOut,
    status_code=201,
    summary="Pay a utility bill (idempotent)",
)
def pay_bill(
    body: PayBillRequest,
    idempotency_key: IdempotencyKeyHeader,
    principal: Principal = Depends(get_current_principal),
    customer: Customer = Depends(get_current_customer),
    service: BillsService = Depends(get_bills_service),
    meta: RequestMeta = Depends(get_request_meta),
) -> JSONResponse:
    result = service.pay_bill(
        user_id=principal.user_id,
        customer=customer,
        command=PayBillCommand(
            from_account_id=body.from_account_id,
            biller_id=body.biller_id,
            consumer_reference=body.consumer_reference,
            amount_minor=body.amount_minor,
            currency=body.currency,
        ),
        idempotency_key=idempotency_key,
        meta=meta,
    )
    return _respond(result)


@router.get(
    "/payments",
    response_model=list[BillPaymentOut],
    summary="List my bill payments",
)
def list_my_bill_payments(
    limit: int = Query(default=50, ge=1, le=200),
    _principal: Principal = Depends(get_current_principal),
    customer: Customer = Depends(get_current_customer),
    service: BillsService = Depends(get_bills_service),
) -> list[dict[str, Any]]:
    return service.list_bill_payments(customer.id, limit=limit)


@router.get(
    "/payments/{payment_id}",
    response_model=BillPaymentOut,
    summary="Get one of my bill payments",
)
def get_my_bill_payment(
    payment_id: UUID,
    _principal: Principal = Depends(get_current_principal),
    customer: Customer = Depends(get_current_customer),
    service: BillsService = Depends(get_bills_service),
) -> JSONResponse:
    return _respond(service.get_bill_payment(customer.id, payment_id))
