from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from pbip_ml.fraud.contract import FraudScorer
from redis import Redis
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import get_session
from pbip_api.core.geoip import CountryResolver
from pbip_api.core.redis import get_redis
from pbip_api.core.velocity import VelocityCounters
from pbip_api.modules.auth.dependencies import (
    Principal,
    get_auth_service,
    get_current_principal,
    get_request_meta,
)
from pbip_api.modules.auth.service import AuthService, RequestMeta
from pbip_api.modules.customers.dependencies import get_current_customer
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.fraud.dependencies import get_country_resolver, get_fraud_scorer
from pbip_api.modules.fraud.signals import device_uuid
from pbip_api.modules.payments.models import ScheduledTransferStatus
from pbip_api.modules.payments.schemas import (
    ScheduledTransferCreate,
    ScheduledTransferOut,
    StepUpRequest,
    TransferOut,
    TransferRequest,
    to_scheduled_transfer_out,
)
from pbip_api.modules.payments.service import PaymentsService, TransferCommand, TransferResult

router = APIRouter(prefix="/api/v1/transfers", tags=["payments"])

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


def get_payments_service(
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    scorer: FraudScorer = Depends(get_fraud_scorer),
    auth: AuthService = Depends(get_auth_service),
    countries: CountryResolver = Depends(get_country_resolver),
    redis_client: Redis = Depends(get_redis),
) -> PaymentsService:
    return PaymentsService(
        session, settings, scorer, auth, countries, VelocityCounters(redis_client)
    )


def _respond(result: TransferResult) -> JSONResponse:
    headers = {"Idempotent-Replayed": "true"} if result.replayed else None
    return JSONResponse(result.body, status_code=result.status_code, headers=headers)


@router.post(
    "",
    response_model=TransferOut,
    status_code=201,
    responses={202: {"model": TransferOut}, 403: {"model": TransferOut}},
    summary="Create a transfer (idempotent)",
)
def create_transfer(
    body: TransferRequest,
    idempotency_key: IdempotencyKeyHeader,
    principal: Principal = Depends(get_current_principal),
    customer: Customer = Depends(get_current_customer),
    service: PaymentsService = Depends(get_payments_service),
    meta: RequestMeta = Depends(get_request_meta),
    x_device_id: str | None = Header(
        default=None,
        alias="X-Device-ID",
        max_length=512,
        description="Per-browser device id (lib/device.ts); only a derived id is used (ADR-0020)",
    ),
) -> JSONResponse:
    result = service.create_transfer(
        user_id=principal.user_id,
        customer=customer,
        command=TransferCommand(
            from_account_id=body.from_account_id,
            amount_minor=body.amount_minor,
            currency=body.currency,
            payee_id=body.payee_id,
            to_account_id=body.to_account_id,
            reference=body.reference,
        ),
        idempotency_key=idempotency_key,
        meta=meta,
        device_id=device_uuid(x_device_id),
    )
    return _respond(result)


# ---------------------------------------------------------------------------
# Scheduled Transfers (declared before /{transfer_id} to avoid UUID collisions)
# ---------------------------------------------------------------------------
@router.post(
    "/scheduled",
    response_model=ScheduledTransferOut,
    status_code=201,
    summary="Create a recurring transfer instruction",
)
def create_scheduled_transfer(
    body: ScheduledTransferCreate,
    principal: Principal = Depends(get_current_principal),
    customer: Customer = Depends(get_current_customer),
    service: PaymentsService = Depends(get_payments_service),
    meta: RequestMeta = Depends(get_request_meta),
) -> ScheduledTransferOut:
    return to_scheduled_transfer_out(
        service.create_scheduled_transfer(
            customer=customer, user_id=principal.user_id, command=body, meta=meta
        )
    )


@router.get(
    "/scheduled",
    response_model=list[ScheduledTransferOut],
    summary="List my scheduled transfers",
)
def list_scheduled_transfers(
    status: ScheduledTransferStatus | None = Query(default=None, description="Filter by status"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    customer: Customer = Depends(get_current_customer),
    service: PaymentsService = Depends(get_payments_service),
) -> list[ScheduledTransferOut]:
    items = service.list_scheduled_transfers(customer.id, status=status, limit=limit, offset=offset)
    return [to_scheduled_transfer_out(st) for st in items]


@router.get(
    "/scheduled/{scheduled_transfer_id}",
    response_model=ScheduledTransferOut,
    summary="Get one of my scheduled transfers",
)
def get_scheduled_transfer(
    scheduled_transfer_id: UUID,
    customer: Customer = Depends(get_current_customer),
    service: PaymentsService = Depends(get_payments_service),
) -> ScheduledTransferOut:
    return to_scheduled_transfer_out(
        service.get_scheduled_transfer(customer.id, scheduled_transfer_id)
    )


@router.post(
    "/scheduled/{scheduled_transfer_id}/cancel",
    response_model=ScheduledTransferOut,
    summary="Cancel a scheduled transfer",
)
@router.delete(
    "/scheduled/{scheduled_transfer_id}",
    response_model=ScheduledTransferOut,
    summary="Cancel a scheduled transfer (DELETE alias)",
)
def cancel_scheduled_transfer(
    scheduled_transfer_id: UUID,
    principal: Principal = Depends(get_current_principal),
    customer: Customer = Depends(get_current_customer),
    service: PaymentsService = Depends(get_payments_service),
    meta: RequestMeta = Depends(get_request_meta),
) -> ScheduledTransferOut:
    return to_scheduled_transfer_out(
        service.cancel_scheduled_transfer(
            customer_id=customer.id,
            user_id=principal.user_id,
            scheduled_transfer_id=scheduled_transfer_id,
            meta=meta,
        )
    )


@router.post(
    "/scheduled/{scheduled_transfer_id}/pause",
    response_model=ScheduledTransferOut,
    summary="Pause an active scheduled transfer",
)
def pause_scheduled_transfer(
    scheduled_transfer_id: UUID,
    principal: Principal = Depends(get_current_principal),
    customer: Customer = Depends(get_current_customer),
    service: PaymentsService = Depends(get_payments_service),
    meta: RequestMeta = Depends(get_request_meta),
) -> ScheduledTransferOut:
    return to_scheduled_transfer_out(
        service.pause_scheduled_transfer(
            customer_id=customer.id,
            user_id=principal.user_id,
            scheduled_transfer_id=scheduled_transfer_id,
            meta=meta,
        )
    )


@router.post(
    "/scheduled/{scheduled_transfer_id}/resume",
    response_model=ScheduledTransferOut,
    summary="Resume a paused scheduled transfer",
)
def resume_scheduled_transfer(
    scheduled_transfer_id: UUID,
    principal: Principal = Depends(get_current_principal),
    customer: Customer = Depends(get_current_customer),
    service: PaymentsService = Depends(get_payments_service),
    meta: RequestMeta = Depends(get_request_meta),
) -> ScheduledTransferOut:
    return to_scheduled_transfer_out(
        service.resume_scheduled_transfer(
            customer=customer,
            user_id=principal.user_id,
            scheduled_transfer_id=scheduled_transfer_id,
            meta=meta,
        )
    )


# ---------------------------------------------------------------------------
# Interactive Single Transfers
# ---------------------------------------------------------------------------
@router.get("/{transfer_id}", response_model=TransferOut, summary="Get one of my transfers")
def get_transfer(
    transfer_id: UUID,
    principal: Principal = Depends(get_current_principal),
    service: PaymentsService = Depends(get_payments_service),
) -> JSONResponse:
    return _respond(service.get_transfer(principal.user_id, transfer_id))


@router.post(
    "/{transfer_id}/step-up",
    response_model=TransferOut,
    summary="Complete a flagged transfer with a TOTP code",
)
def confirm_step_up(
    transfer_id: UUID,
    body: StepUpRequest,
    principal: Principal = Depends(get_current_principal),
    service: PaymentsService = Depends(get_payments_service),
    meta: RequestMeta = Depends(get_request_meta),
) -> JSONResponse:
    return _respond(
        service.confirm_step_up(
            user_id=principal.user_id, transaction_id=transfer_id, code=body.code, meta=meta
        )
    )
