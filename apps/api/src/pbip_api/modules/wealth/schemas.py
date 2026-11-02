"""Wealth API contracts (TASK-063, TASK-064). Money is integer minor units (paise); rates are
basis points (710 = 7.10%)."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

MAX_AMOUNT_MINOR = 10_00_00_000_00  # INR 10 crore: above every catalogue maximum

RiskLevelLiteral = Literal["LOW", "MODERATE", "HIGH"]


class WealthProductOut(BaseModel):
    id: UUID
    code: str
    product_type: Literal["FIXED_DEPOSIT", "MUTUAL_FUND", "SOVEREIGN_GOLD_BOND"]
    name: str
    description: str
    risk_level: RiskLevelLiteral
    yield_basis: Literal["GUARANTEED", "FIXED_COUPON", "MARKET_LINKED"]
    annual_rate_bps: int | None = Field(
        description="Indicative, simulated annual rate (deposits) or coupon (gold bond); "
        "None when market-linked"
    )
    apy_bps: int | None = Field(
        description="Annualised yield: quarterly compounding for deposits, the coupon for the gold "
        "bond; None for market-linked funds (PBIP has no market data)"
    )
    tenures_months: list[int] = Field(description="Empty for open-ended funds")
    min_lock_in_months: int
    min_amount_minor: int | None
    max_amount_minor: int | None
    premature_closure_allowed: bool
    premature_penalty_bps: int | None
    bookable: bool = Field(description="Only fixed deposits can be booked online")
    notes: str
    catalogue_version: str


class DepositTermsRequest(BaseModel):
    """No rate, maturity or customer from the client: the rate comes from the catalogue, the
    maturity from ``deposit_math`` and the customer from the session (ADR-0025)."""

    model_config = ConfigDict(extra="forbid")

    product_id: UUID
    amount_minor: int = Field(gt=0, le=MAX_AMOUNT_MINOR)
    tenure_months: int = Field(gt=0, le=120)


class DepositQuoteOut(BaseModel):
    product_id: UUID
    product_name: str
    principal_minor: int
    tenure_months: int
    annual_rate_bps: int
    apy_bps: int
    start_date: date
    maturity_date: date
    maturity_interest_minor: int
    maturity_amount_minor: int
    lock_in_months: int
    premature_penalty_bps: int | None


class DepositBookRequest(DepositTermsRequest):
    account_id: UUID = Field(
        description="One of MY accounts: funds the deposit, receives the payout"
    )


class FixedDepositOut(BaseModel):
    id: UUID
    product_id: UUID
    product_name: str
    status: Literal["ACTIVE", "MATURED", "CLOSED"] = Field(
        description="MATURED = active and the maturity date reached: ready to withdraw"
    )
    account_id: UUID
    currency: str
    principal_minor: int
    annual_rate_bps: int
    tenure_months: int
    start_date: date
    maturity_date: date
    days_to_maturity: int = Field(description="Bank days until maturity; 0 once matured")
    maturity_interest_minor: int
    maturity_amount_minor: int
    accrued_interest_minor: int = Field(
        description="Interest earned to today at the contract rate (paid interest once closed)"
    )
    lock_in_until: date
    can_close_now: bool
    payout_if_closed_today_minor: int | None = Field(
        description="What closing today would pay (premature penalty applied); None if it cannot"
    )
    closure_kind: Literal["MATURED", "PREMATURE"] | None
    paid_interest_minor: int | None
    closed_on: date | None
    booking_transaction_id: UUID
    closure_transaction_id: UUID | None
    interest_transaction_id: UUID | None
    booked_at: datetime


class AllocationSlice(BaseModel):
    asset_class: Literal["CASH", "DEPOSITS", "FUNDS"]
    value_minor: int
    share_bps: int = Field(description="Share of the total; the slices sum to 10000 (0 if empty)")


class PortfolioOut(BaseModel):
    currency: Literal["INR"]
    as_of: date
    cash_minor: int = Field(description="Ledger balance of my INR accounts")
    invested_principal_minor: int = Field(description="Principal of my open fixed deposits")
    accrued_interest_minor: int = Field(description="Interest earned to date on open deposits")
    deposits_value_minor: int = Field(description="Principal + accrued interest")
    projected_maturity_value_minor: int
    funds_minor: int = Field(description="Fund holdings: PBIP sells no fund units, so always 0")
    total_minor: int
    open_deposits: int
    allocation: list[AllocationSlice]
