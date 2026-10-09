from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from pbip_api.core.errors import NotFoundError
from pbip_api.modules.payees.models import Payee


def get_owned_payee(session: Session, customer_id: UUID, payee_id: UUID) -> Payee:
    payee = session.execute(
        select(Payee).where(Payee.id == payee_id, Payee.customer_id == customer_id)
    ).scalar_one_or_none()
    if payee is None:
        raise NotFoundError("payee not found")
    return payee
