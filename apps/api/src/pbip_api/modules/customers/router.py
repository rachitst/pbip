"""Customer profile and device registration routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy.orm import Session

from pbip_api.core.db import get_session
from pbip_api.core.errors import UnprocessableError
from pbip_api.modules.auth.dependencies import get_request_meta
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.customers import policy
from pbip_api.modules.customers.dependencies import get_current_customer, get_customer_service
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.customers.schemas import CustomerProfileOut, DeviceOut, DeviceRegisterRequest
from pbip_api.modules.customers.service import CustomerService, to_device_out

router = APIRouter(prefix="/api/v1/customers", tags=["customers"])


@router.get("/me", response_model=CustomerProfileOut, summary="Get my customer profile")
def get_my_profile(
    customer: Customer = Depends(get_current_customer),
    service: CustomerService = Depends(get_customer_service),
) -> CustomerProfileOut:
    return service.get_profile(customer)


@router.post(
    "/devices",
    response_model=DeviceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register or touch active device fingerprint",
)
def register_device(
    response: Response,
    body: DeviceRegisterRequest,
    x_device_id: str | None = Header(default=None, alias="X-Device-ID", max_length=512),
    customer: Customer = Depends(get_current_customer),
    service: CustomerService = Depends(get_customer_service),
    meta: RequestMeta = Depends(get_request_meta),
) -> DeviceOut:
    raw_fingerprint = body.fingerprint or x_device_id
    if not raw_fingerprint or not raw_fingerprint.strip():
        raise UnprocessableError("device fingerprint or X-Device-ID header is required")

    device, is_new = service.register_device(
        customer=customer,
        raw_fingerprint=raw_fingerprint.strip(),
        label=body.label,
        meta=meta,
    )
    if not is_new:
        response.status_code = status.HTTP_200_OK
    return to_device_out(device)


@router.get("/devices", response_model=list[DeviceOut], summary="List my registered devices")
def list_my_devices(
    customer: Customer = Depends(get_current_customer),
    service: CustomerService = Depends(get_customer_service),
) -> list[DeviceOut]:
    return [to_device_out(d) for d in service.list_devices(customer.id)]


@router.get(
    "/devices/{device_id}",
    response_model=DeviceOut,
    summary="Get one of my registered devices",
)
def get_my_device(
    device_id: UUID,
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
) -> DeviceOut:
    return to_device_out(policy.get_owned_device(session, customer.id, device_id))
