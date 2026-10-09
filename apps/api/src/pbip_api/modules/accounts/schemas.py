from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


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
    next_cursor: str | None
