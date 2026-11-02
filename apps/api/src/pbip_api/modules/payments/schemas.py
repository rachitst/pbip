from datetime import date, datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from pbip_api.core.validation import Text
from pbip_api.modules.payments.models import (
    ScheduledTransfer,
    ScheduledTransferStatus,
    ScheduleFrequency,
)


class TransferRequest(BaseModel):
    from_account_id: UUID
    amount_minor: int = Field(gt=0, description="Integer minor units, e.g. paise")
    currency: str = Field(default="INR", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    payee_id: UUID | None = None
    to_account_id: UUID | None = Field(default=None, description="One of MY OWN other accounts")
    reference: Text | None = Field(default=None, max_length=140)

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


class ScheduledTransferCreate(BaseModel):
    from_account_id: UUID
    amount_minor: int = Field(gt=0, description="Integer minor units, e.g. paise")
    currency: str = Field(default="INR", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    payee_id: UUID | None = None
    to_account_id: UUID | None = Field(default=None, description="One of MY OWN other accounts")
    reference: Text | None = Field(default=None, max_length=140)
    frequency: ScheduleFrequency
    start_date: date = Field(description="First scheduled date (UTC), must be on or after today")
    end_date: date | None = Field(default=None, description="Optional terminal date (inclusive)")

    @model_validator(mode="after")
    def _validate_destinations_and_dates(self) -> Self:
        if (self.payee_id is None) == (self.to_account_id is None):
            raise ValueError("provide exactly one of payee_id or to_account_id")
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class ScheduledTransferOut(BaseModel):
    id: UUID
    customer_id: UUID
    from_account_id: UUID
    to_account_id: UUID | None
    payee_id: UUID | None
    amount_minor: int
    currency: str
    reference: str | None
    frequency: ScheduleFrequency
    status: ScheduledTransferStatus
    start_date: date
    end_date: date | None
    next_run_date: date
    last_run_date: date | None
    last_transaction_id: UUID | None
    failure_count: int
    last_failure_reason: str | None
    pause_reason: str | None
    created_at: datetime


def to_scheduled_transfer_out(st: ScheduledTransfer) -> ScheduledTransferOut:
    return ScheduledTransferOut(
        id=st.id,
        customer_id=st.customer_id,
        from_account_id=st.from_account_id,
        to_account_id=st.to_account_id,
        payee_id=st.payee_id,
        amount_minor=st.amount,
        currency=st.currency,
        reference=st.reference,
        frequency=st.frequency,
        status=st.status,
        start_date=st.start_date,
        end_date=st.end_date,
        next_run_date=st.next_run_date,
        last_run_date=st.last_run_date,
        last_transaction_id=st.last_transaction_id,
        failure_count=st.failure_count,
        last_failure_reason=st.last_failure_reason,
        pause_reason=st.pause_reason,
        created_at=st.created_at,
    )
