from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import get_session
from pbip_api.modules.accounts.service import mask_account_number
from pbip_api.modules.customers.dependencies import get_current_customer
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.payees import policy
from pbip_api.modules.payees.models import Payee
from pbip_api.modules.payees.schemas import PayeeCreate, PayeeOut, PayeeVerificationOut
from pbip_api.modules.payees.service import PayeeService

router = APIRouter(prefix="/api/v1/payees", tags=["payees"])


def get_payee_service(
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> PayeeService:
    return PayeeService(session, settings)


def to_out(payee: Payee) -> PayeeOut:
    return PayeeOut(
        id=payee.id,
        name=payee.name,
        account_number_masked=mask_account_number(payee.account_number),
        bank_code=payee.bank_code,
        nickname=payee.nickname,
        status=payee.status.value,
        created_at=payee.created_at,
        verified_at=payee.verified_at,
    )


@router.post(
    "", response_model=PayeeOut, status_code=status.HTTP_201_CREATED, summary="Add a payee"
)
def create_payee(
    body: PayeeCreate,
    customer: Customer = Depends(get_current_customer),
    service: PayeeService = Depends(get_payee_service),
) -> PayeeOut:
    return to_out(
        service.create(customer.id, body.name, body.account_number, body.bank_code, body.nickname)
    )


@router.get("", response_model=list[PayeeOut], summary="List my payees")
def list_payees(
    customer: Customer = Depends(get_current_customer),
    service: PayeeService = Depends(get_payee_service),
) -> list[PayeeOut]:
    return [to_out(p) for p in service.list(customer.id)]


@router.get("/{payee_id}", response_model=PayeeOut, summary="Get one of my payees")
def get_payee(
    payee_id: UUID,
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
) -> PayeeOut:
    return to_out(policy.get_owned_payee(session, customer.id, payee_id))


@router.post(
    "/{payee_id}/verify",
    response_model=PayeeVerificationOut,
    summary="Confirmation-of-payee style name check; verified payees can receive transfers",
)
def verify_payee(
    payee_id: UUID,
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
    service: PayeeService = Depends(get_payee_service),
) -> PayeeVerificationOut:
    payee = policy.get_owned_payee(session, customer.id, payee_id)
    match = service.verify(payee)
    return PayeeVerificationOut(payee=to_out(payee), name_match=match)


@router.delete("/{payee_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Remove a payee")
def delete_payee(
    payee_id: UUID,
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
    service: PayeeService = Depends(get_payee_service),
) -> None:
    service.delete(policy.get_owned_payee(session, customer.id, payee_id))
