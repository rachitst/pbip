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
