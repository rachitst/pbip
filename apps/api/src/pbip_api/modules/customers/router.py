"""Customer profile and device registration routes."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response, status
from redis import Redis
from sqlalchemy.orm import Session

from pbip_api.core.calendar import bank_today
from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import get_session
from pbip_api.core.errors import UnprocessableError
from pbip_api.core.ratelimit import consume
from pbip_api.core.redis import get_redis
from pbip_api.modules.auth.dependencies import get_clock, get_request_meta
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.customers import policy
from pbip_api.modules.customers.dependencies import get_current_customer, get_customer_service
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.customers.schemas import (
    CustomerProfileOut,
    DateOfBirthIn,
    DemographicsOut,
    DeviceOut,
    DeviceRegisterRequest,
    PhoneIn,
    RecommendationsConsentIn,
    RiskProfileOut,
    RiskQuestionnaire,
)
from pbip_api.modules.customers.service import CustomerService, to_device_out

router = APIRouter(prefix="/api/v1/customers", tags=["customers"])


@router.get("/me", response_model=CustomerProfileOut, summary="Get my customer profile")
def get_my_profile(
    customer: Customer = Depends(get_current_customer),
    service: CustomerService = Depends(get_customer_service),
) -> CustomerProfileOut:
    return service.get_profile(customer)


def _now(clock: Callable[[], float]) -> datetime:
    return datetime.fromtimestamp(clock(), tz=UTC)


_PHONE_WINDOW_SECONDS = 3600


def phone_change_rate_limit(
    customer: Customer = Depends(get_current_customer),
    redis_client: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> None:
    """Per customer, not per IP: one account must not be able to churn its number."""
    consume(
        redis_client,
        "phone_change",
        f"user:{customer.user_id}",
        settings.rate_limit_phone_change_per_hour,
        _PHONE_WINDOW_SECONDS,
    )


@router.put(
    "/me/phone",
    response_model=CustomerProfileOut,
    summary="Set my mobile number (E.164); stored encrypted, returned masked",
)
def put_my_phone(
    body: PhoneIn,
    _limited: None = Depends(phone_change_rate_limit),
    customer: Customer = Depends(get_current_customer),
    service: CustomerService = Depends(get_customer_service),
    meta: RequestMeta = Depends(get_request_meta),
) -> CustomerProfileOut:
    return service.set_phone(customer, body.phone, meta)


@router.delete(
    "/me/phone", status_code=status.HTTP_204_NO_CONTENT, summary="Delete my mobile number"
)
def delete_my_phone(
    _limited: None = Depends(phone_change_rate_limit),
    customer: Customer = Depends(get_current_customer),
    service: CustomerService = Depends(get_customer_service),
    meta: RequestMeta = Depends(get_request_meta),
) -> None:
    service.delete_phone(customer, meta)


@router.get(
    "/me/risk-profile",
    response_model=RiskProfileOut,
    summary="Get my risk-tolerance profile (band, answers and when it was assessed)",
)
def get_my_risk_profile(
    customer: Customer = Depends(get_current_customer),
    service: CustomerService = Depends(get_customer_service),
) -> RiskProfileOut:
    return service.get_risk_profile(customer)


@router.put(
    "/me/risk-profile",
    response_model=RiskProfileOut,
    summary="Answer the risk-tolerance questionnaire and store the resulting band",
)
def put_my_risk_profile(
    body: RiskQuestionnaire,
    customer: Customer = Depends(get_current_customer),
    service: CustomerService = Depends(get_customer_service),
    meta: RequestMeta = Depends(get_request_meta),
    clock: Callable[[], float] = Depends(get_clock),
) -> RiskProfileOut:
    return service.assess_risk_profile(customer, body, _now(clock), meta)


@router.get(
    "/me/demographics",
    response_model=DemographicsOut,
    summary="Get my date of birth, derived age band and recommendations consent",
)
def get_my_demographics(
    customer: Customer = Depends(get_current_customer),
    service: CustomerService = Depends(get_customer_service),
    clock: Callable[[], float] = Depends(get_clock),
) -> DemographicsOut:
    return service.get_demographics(customer, bank_today(_now(clock)))


@router.put(
    "/me/date-of-birth",
    response_model=DemographicsOut,
    summary="Set my date of birth (optional; age 18 to 120)",
)
def put_my_date_of_birth(
    body: DateOfBirthIn,
    customer: Customer = Depends(get_current_customer),
    service: CustomerService = Depends(get_customer_service),
    meta: RequestMeta = Depends(get_request_meta),
    clock: Callable[[], float] = Depends(get_clock),
) -> DemographicsOut:
    return service.set_date_of_birth(customer, body.date_of_birth, _now(clock), meta)


@router.delete(
    "/me/date-of-birth", status_code=status.HTTP_204_NO_CONTENT, summary="Delete my date of birth"
)
def delete_my_date_of_birth(
    customer: Customer = Depends(get_current_customer),
    service: CustomerService = Depends(get_customer_service),
    meta: RequestMeta = Depends(get_request_meta),
) -> None:
    service.delete_date_of_birth(customer, meta)


@router.put(
    "/me/recommendations-consent",
    response_model=DemographicsOut,
    summary="Grant or withdraw consent to use my age band for product recommendations",
)
def put_my_recommendations_consent(
    body: RecommendationsConsentIn,
    customer: Customer = Depends(get_current_customer),
    service: CustomerService = Depends(get_customer_service),
    meta: RequestMeta = Depends(get_request_meta),
    clock: Callable[[], float] = Depends(get_clock),
) -> DemographicsOut:
    return service.set_recommendations_consent(customer, body.granted, _now(clock), meta)


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
