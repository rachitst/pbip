"""Loan HTTP routes (TASK-045, TASK-046). Customer-only; nothing names another customer."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from pbip_api.core.db import get_session
from pbip_api.modules.auth.dependencies import Principal, get_current_principal
from pbip_api.modules.customers.dependencies import get_current_customer
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.loans import service
from pbip_api.modules.loans.schemas import (
    EligibilityOut,
    EligibilityRequest,
    EmiOut,
    EmiRequest,
    LoanProductOut,
)

router = APIRouter(prefix="/api/v1/loans", tags=["loans"])


@router.get("/products", response_model=list[LoanProductOut], summary="Loan catalogue")
def list_products(_customer: Customer = Depends(get_current_customer)) -> list[LoanProductOut]:
    return service.list_products()


@router.post("/calculate-emi", response_model=EmiOut, summary="EMI and repayment schedule")
def calculate_emi(body: EmiRequest, _customer: Customer = Depends(get_current_customer)) -> EmiOut:
    return service.calculate_emi(body.product_id, body.principal_minor, body.tenure_months)


@router.post(
    "/check-eligibility",
    response_model=EligibilityOut,
    summary="Explainable eligibility from the customer's own ledger figures",
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
