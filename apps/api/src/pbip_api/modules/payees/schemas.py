from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class PayeeCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    account_number: str = Field(pattern=r"^[0-9A-Za-z-]{6,20}$")
    bank_code: str = Field(default="PBIP0001", pattern=r"^[0-9A-Za-z]{4,20}$")
    nickname: str | None = Field(default=None, max_length=80)


class PayeeOut(BaseModel):
    id: UUID
    name: str
    account_number_masked: str
    bank_code: str
    nickname: str | None
    status: str
    created_at: datetime
    verified_at: datetime | None


class PayeeVerificationOut(BaseModel):
    payee: PayeeOut
    name_match: Literal["MATCH", "NO_MATCH", "NOT_CHECKED"]
