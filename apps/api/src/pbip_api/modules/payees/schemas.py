from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from pbip_api.core.validation import Text


class PayeeCreate(BaseModel):
    name: Text = Field(min_length=2, max_length=200)
    account_number: str = Field(pattern=r"^[0-9A-Za-z-]{6,20}$")
    bank_code: str = Field(default="PBIP0001", pattern=r"^[0-9A-Za-z]{4,20}$")
    nickname: Text | None = Field(default=None, max_length=80)


class PayeeUpdate(BaseModel):
    """Only the name and nickname can change. Account number and bank code are not accepted at
    all (``extra="forbid"`` -> 422): a different account is a different payee."""

    model_config = ConfigDict(extra="forbid")

    name: Text | None = Field(default=None, min_length=2, max_length=200)
    nickname: Text | None = Field(default=None, max_length=80)


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
