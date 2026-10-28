"""Object-level authorization policy for in-app notifications."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from pbip_api.core.errors import NotFoundError
from pbip_api.modules.notifications.models import Notification


def get_owned_notification(session: Session, user_id: UUID, notification_id: UUID) -> Notification:
    """Fetch a notification strictly owned by the caller.

    Missing notifications and notifications belonging to another user both raise NotFoundError
    (HTTP 404) to avoid ID enumeration and probing.
    """
    notification = session.execute(
        select(Notification).where(
            Notification.id == notification_id, Notification.user_id == user_id
        )
    ).scalar_one_or_none()
    if notification is None:
        raise NotFoundError("notification not found")
    return notification
