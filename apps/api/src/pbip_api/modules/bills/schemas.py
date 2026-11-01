"""Pydantic request and response schemas for billers and bill payments."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from pbip_api.core.validation import Text
from pbip_api.modules.bills.models import BillerCategory, BillerStatus


class BillerOut(BaseModel):
    id: UUID
    name: str
    category: BillerCategory
    biller_code: str
    status: BillerStatus


class PayBillRequest(BaseModel):
    from_account_id: UUID
    biller_id: UUID
    consumer_reference: Text = Field(
        min_length=2,
        max_length=64,
        description="Customer consumer/subscriber number with the utility biller",
    )
    amount_minor: int = Field(
        gt=0,
        le=9_223_372_036_854_775_807,
        description="Integer minor units, e.g. paise",
    )
    currency: str = Field(default="INR", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")


class BillPaymentOut(BaseModel):
    id: UUID
    customer_id: UUID
    account_id: UUID
    biller_id: UUID
    biller_name: str
    biller_code: str
    transaction_id: UUID
    consumer_reference: str
    amount_minor: int
    currency: str
    status: Literal["POSTED", "PENDING_REVIEW", "DECLINED", "REVERSED"]
    reference: str
    new_balance_minor: int
    created_at: datetime
    message: str
