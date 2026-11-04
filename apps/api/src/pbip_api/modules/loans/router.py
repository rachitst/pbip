"""Loan HTTP routes (TASK-045, TASK-046, TASK-047). Customer-only; nothing names another
customer: the customer is always the authenticated one, and loans are read via ``loans/policy``."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import get_session
from pbip_api.modules.auth.dependencies import Principal, get_current_principal, get_request_meta
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.customers.dependencies import get_current_customer
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.loans import service
from pbip_api.modules.loans.schemas import (
    EligibilityOut,
    EligibilityRequest,
    EmiOut,
    EmiRequest,
    LoanApplyRequest,
    LoanOut,
    LoanProductOut,
    LoanSummaryOut,
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

router = APIRouter(prefix="/api/v1/loans", tags=["loans"])


@router.get("/products", response_model=list[LoanProductOut], summary="Loan catalogue")
def list_products(_customer: Customer = Depends(get_current_customer)) -> list[LoanProductOut]:
    return service.list_products()


@router.post(
    "/calculate-emi",
    response_model=EmiOut,
    summary="EMI and repayment schedule",
    responses={404: {"description": "No such loan product (problem+json)"}},
)
def calculate_emi(body: EmiRequest, _customer: Customer = Depends(get_current_customer)) -> EmiOut:
    return service.calculate_emi(body.product_id, body.principal_minor, body.tenure_months)


@router.post(
    "/check-eligibility",
    response_model=EligibilityOut,
    summary="Explainable eligibility from the customer's own ledger figures",
    responses={404: {"description": "No such loan product (problem+json)"}},
)
def check_eligibility(
    body: EligibilityRequest,
    customer: Customer = Depends(get_current_customer),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_session),
) -> EligibilityOut:
    return service.check_eligibility(
        session,
        customer_id=customer.id,
        user_id=principal.user_id,
        product_id=body.product_id,
        amount_minor=body.amount_minor,
        tenure_months=body.tenure_months,
    )


@router.post(
    "/apply",
    response_model=LoanOut,
    status_code=201,
    summary="Apply for a loan: approve, book the schedule and disburse (idempotent)",
    responses={
        404: {"description": "No such loan product, or the account is not mine (problem+json)"},
        422: {"description": "Not eligible (plain reasons) or invalid request"},
    },
)
def apply_for_loan(
    body: LoanApplyRequest,
    idempotency_key: IdempotencyKeyHeader,
    customer: Customer = Depends(get_current_customer),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    meta: RequestMeta = Depends(get_request_meta),
) -> JSONResponse:
    result = service.apply_for_loan(
        session,
        settings,
        customer_id=customer.id,
        user_id=principal.user_id,
        request=body,
        idempotency_key=idempotency_key,
        meta=meta,
    )
    headers = {"Idempotent-Replayed": "true"} if result.replayed else None
    return JSONResponse(result.body, status_code=result.status_code, headers=headers)


@router.get("", response_model=list[LoanSummaryOut], summary="My active loans")
def list_loans(
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
) -> list[LoanSummaryOut]:
    return service.list_loans(session, customer.id)


@router.get(
    "/{loan_id}", response_model=LoanOut, summary="One of my loans with its repayment schedule"
)
def get_loan(
    loan_id: UUID,
    customer: Customer = Depends(get_current_customer),
    session: Session = Depends(get_session),
) -> LoanOut:
    return service.get_loan(session, customer.id, loan_id)
