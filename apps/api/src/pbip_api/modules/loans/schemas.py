"""Loan API contracts (TASK-045, TASK-046). Money is integer minor units (paise)."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

MAX_AMOUNT_MINOR = 10_00_00_000_00  # INR 10 crore


class LoanProductOut(BaseModel):
    id: UUID
    loan_type: Literal["PERSONAL", "AUTO", "HOME"]
    name: str
    annual_rate_bps: int = Field(
        description="Indicative annual rate in basis points (1150 = 11.50%)"
    )
    min_amount_minor: int
    max_amount_minor: int
    tenures_months: list[int]


class EmiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: UUID
    principal_minor: int = Field(gt=0, le=MAX_AMOUNT_MINOR)
    tenure_months: int = Field(gt=0, le=480)


class InstalmentOut(BaseModel):
    month: int
    payment_minor: int
    principal_minor: int
    interest_minor: int
    balance_after_minor: int


class EmiOut(BaseModel):
    product_id: UUID
    annual_rate_bps: int
    principal_minor: int
    tenure_months: int
    emi_minor: int
    total_interest_minor: int
    total_payable_minor: int
    schedule: list[InstalmentOut]


class EligibilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: UUID
    amount_minor: int = Field(gt=0, le=MAX_AMOUNT_MINOR)
    tenure_months: int = Field(gt=0, le=480)


class TenorOption(BaseModel):
    tenure_months: int
    max_amount_minor: int
    emi_at_max_minor: int


class EligibilityOut(BaseModel):
    status: Literal["ELIGIBLE", "NOT_ELIGIBLE"]
    product_id: UUID
    requested_amount_minor: int
    tenure_months: int
    monthly_income_minor: int = Field(description="Average monthly income from the ledger")
    existing_emi_minor: int = Field(description="Loan repayments already being made each month")
    proposed_emi_minor: int
    dti_existing: float = Field(description="Existing repayments / income")
    dti_with_loan: float = Field(description="(Existing + proposed EMI) / income; limit 0.50")
    max_eligible_amount_minor: int = Field(description="Largest amount at the requested tenure")
    tenor_options: list[TenorOption]
    reasons: list[str] = Field(min_length=1, description="Why, in plain words with the figures")
