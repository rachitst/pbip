"""Object-level authorization for bill payments.

A missing bill payment and another customer's bill payment both raise NotFoundError (404),
preventing BOLA/IDOR enumeration of payment receipts.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from pbip_api.core.errors import NotFoundError
from pbip_api.modules.bills.models import BillPayment


def get_owned_bill_payment(
    session: Session, customer_id: UUID, bill_payment_id: UUID
) -> BillPayment:
    payment = session.execute(
        select(BillPayment).where(
            BillPayment.id == bill_payment_id,
            BillPayment.customer_id == customer_id,
        )
    ).scalar_one_or_none()
    if payment is None:
        raise NotFoundError("bill payment not found")
    return payment
