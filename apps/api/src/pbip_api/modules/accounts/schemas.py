from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class OpenAccountType(StrEnum):
    SAVINGS = "SAVINGS"
    CHECKING = "CHECKING"
    CURRENT = "CURRENT"


class SupportedCurrency(StrEnum):
    """Single-currency bank (blueprint section 7): the clearing account and every rail are INR."""

    INR = "INR"


class OpenAccountRequest(BaseModel):
    account_type: OpenAccountType
    currency: SupportedCurrency = SupportedCurrency.INR

    @field_validator("account_type", mode="before")
    @classmethod
    def _normalize_account_type(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip().upper()
        return v


class AccountOut(BaseModel):
    id: UUID
    account_number: str
    account_number_masked: str
    account_type: str
    currency: str
    status: str
    ledger_balance_minor: int
    held_minor: int
    available_minor: int


class CurrencyTotalOut(BaseModel):
    """Totals for ONE currency: amounts in different currencies are never added together."""

    currency: str
    ledger_balance_minor: int = Field(
        description="Posted balance of the counted ACTIVE and FROZEN accounts"
    )
    held_minor: int = Field(description="Amount on hold (pending review) on the counted accounts")
    available_minor: int = Field(
        description="Ledger minus held, ACTIVE accounts only: held amounts and frozen accounts "
        "are excluded; an arranged overdraft is not added"
    )
    account_count: int = Field(ge=0)
    frozen_account_count: int = Field(ge=0)


class AccountsSummaryOut(BaseModel):
    """FR-08 total balance overview. ``totals`` is empty when the customer holds no counted
    account; it has one row per currency, ordered by currency code."""

    totals: list[CurrencyTotalOut]


class EntryOut(BaseModel):
    entry_seq: int
    transaction_id: UUID
    direction: str
    amount_minor: int
    balance_after_minor: int
    transaction_type: str
    transaction_status: str
    reference: str | None
    created_at: datetime


class EntriesPage(BaseModel):
    items: list[EntryOut]
    next_cursor: str | None = Field(
        description="Opaque cursor for the next page in the same direction; null on the last page"
    )
    has_more: bool
