"""Object-level authorization for scheduled transfers (ADR-0021).

The ONLY way application code obtains a scheduled transfer on behalf of a customer.
A missing instruction and someone else's instruction are indistinguishable (404),
so IDs cannot be probed.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from pbip_api.core.errors import NotFoundError
from pbip_api.modules.payments.models import ScheduledTransfer


def get_owned_scheduled_transfer(
    session: Session,
    customer_id: UUID,
    scheduled_transfer_id: UUID,
    *,
    for_update: bool = False,
) -> ScheduledTransfer:
    """Fetch an owned scheduled transfer instruction.

    Non-owned and non-existent are indistinguishable (404 NotFoundError).
    """
    stmt = select(ScheduledTransfer).where(
        ScheduledTransfer.id == scheduled_transfer_id,
        ScheduledTransfer.customer_id == customer_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    row = session.execute(stmt).scalar_one_or_none()
    if row is None:
        raise NotFoundError("scheduled transfer not found")
    return row
