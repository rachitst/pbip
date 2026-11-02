"""Object-level authorization for fixed deposits (TASK-064).

The ONLY way application code obtains a deposit on behalf of a customer. Someone else's deposit and
a deposit that does not exist are indistinguishable (404), so deposit ids cannot be probed.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from pbip_api.core.errors import NotFoundError
from pbip_api.modules.wealth.models import FixedDeposit


def get_owned_fixed_deposit(
    session: Session, customer_id: UUID, deposit_id: UUID, *, for_update: bool = False
) -> FixedDeposit:
    stmt = select(FixedDeposit).where(
        FixedDeposit.id == deposit_id, FixedDeposit.customer_id == customer_id
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    deposit = session.execute(stmt).scalar_one_or_none()
    if deposit is None:
        raise NotFoundError("fixed deposit not found")
    return deposit


def list_owned_fixed_deposits(session: Session, customer_id: UUID) -> list[FixedDeposit]:
    stmt = (
        select(FixedDeposit)
        .where(FixedDeposit.customer_id == customer_id)
        .order_by(FixedDeposit.created_at.desc(), FixedDeposit.id.desc())
    )
    return list(session.execute(stmt).scalars())
