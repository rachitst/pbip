from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class TransferRequest(BaseModel):
    from_account_id: UUID
    amount_minor: int = Field(gt=0, description="Integer minor units, e.g. paise")
    currency: str = Field(default="INR", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    payee_id: UUID | None = None
    to_account_id: UUID | None = Field(default=None, description="One of MY OWN other accounts")
    reference: str | None = Field(default=None, max_length=140)

    @model_validator(mode="after")
    def _exactly_one_destination(self) -> Self:
        if (self.payee_id is None) == (self.to_account_id is None):
            raise ValueError("provide exactly one of payee_id or to_account_id")
        return self


class StepUpRequest(BaseModel):
    code: str = Field(min_length=6, max_length=8, pattern=r"^[0-9]+$")


class TransferOut(BaseModel):
    id: UUID
    status: Literal["PENDING_STEP_UP", "PENDING_REVIEW", "POSTED", "DECLINED", "REVERSED"]
    amount_minor: int
    currency: str
    from_account_id: UUID | None
    to_account_id: UUID | None
    payee_id: UUID | None
    reference: str | None
    created_at: datetime
    posted_at: datetime | None
    step_up_required: bool
    message: str
