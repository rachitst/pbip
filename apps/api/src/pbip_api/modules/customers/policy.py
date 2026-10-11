"""Object-level authorization policy for devices."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from pbip_api.core.errors import NotFoundError
from pbip_api.modules.customers.models import Device


def get_owned_device(session: Session, customer_id: UUID, device_id: UUID) -> Device:
    """Fetch device strictly owned by the caller.

    Missing devices and devices owned by other users return 404 to prevent ID probing.
    """
    device = session.execute(
        select(Device).where(Device.id == device_id, Device.customer_id == customer_id)
    ).scalar_one_or_none()
    if device is None:
        raise NotFoundError("device not found")
    return device
