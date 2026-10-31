"""Transactional outbox + in-app notifications."""

from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from pbip_api.core.db import utcnow
from pbip_api.core.money import format_minor
from pbip_api.modules.notifications.models import Notification, OutboxEvent
from pbip_api.modules.notifications.policy import get_owned_notification

TRANSFER_POSTED = "TRANSFER_POSTED"
TRANSFER_DECLINED = "TRANSFER_DECLINED"
TRANSFER_UNDER_REVIEW = "TRANSFER_UNDER_REVIEW"
BILL_PAYMENT_POSTED = "BILL_PAYMENT_POSTED"
BILL_PAYMENT_DECLINED = "BILL_PAYMENT_DECLINED"
# Security notifications for events the auth and customers modules already record (#45).
PASSWORD_CHANGED = "PASSWORD_CHANGED"  # noqa: S105  # pragma: allowlist secret - an event type name
MFA_ENABLED = "MFA_ENABLED"
DEVICE_REGISTERED = "DEVICE_REGISTERED"

_TRANSFER_TYPES = {
    TRANSFER_POSTED,
    TRANSFER_UNDER_REVIEW,
    TRANSFER_DECLINED,
    BILL_PAYMENT_POSTED,
    BILL_PAYMENT_DECLINED,
}
_SECURITY_TEXT = {
    PASSWORD_CHANGED: (
        "Password changed",
        "Your password was changed and other sessions were signed out. If this wasn't you, "
        "contact the bank.",
    ),
    MFA_ENABLED: (
        "Two-step verification turned on",
        "Sign-ins now need a code from your authenticator app.",
    ),
    DEVICE_REGISTERED: (
        "New device registered",
        "A new device was registered on your account. If this wasn't you, contact the bank.",
    ),
}


def enqueue(session: Session, event_type: str, payload: dict[str, Any]) -> None:
    """Add an outbox event in the CALLER'S transaction (no PII beyond ids and amounts)."""
    session.add(OutboxEvent(event_type=event_type, payload=payload))


def enqueue_security(session: Session, event_type: str, user_id: UUID) -> None:
    """A security notification for ``user_id``, committed with the change that caused it."""
    if event_type not in _SECURITY_TEXT:
        raise ValueError(f"not a security notification type: {event_type}")
    enqueue(session, event_type, {"user_id": str(user_id)})


def _transfer_text(event: OutboxEvent) -> tuple[str, str] | None:
    amount = format_minor(int(event.payload["amount_minor"]), str(event.payload["currency"]))
    if event.event_type == TRANSFER_POSTED:
        return "Transfer completed", f"Your transfer of {amount} was completed."
    if event.event_type == TRANSFER_UNDER_REVIEW:
        return (
            "Transfer under review",
            f"Your transfer of {amount} is being reviewed for your safety.",
        )
    if event.event_type == TRANSFER_DECLINED:
        return "Transfer not completed", f"Your transfer of {amount} could not be completed."
    name = event.payload.get("biller_name")
    to_biller = f" to {name}" if isinstance(name, str) and name else ""
    if event.event_type == BILL_PAYMENT_POSTED:
        return "Bill payment completed", f"Your bill payment of {amount}{to_biller} was completed."
    if event.event_type == BILL_PAYMENT_DECLINED:
        return (
            "Bill payment not completed",
            f"Your bill payment of {amount}{to_biller} could not be completed.",
        )
    return None


def _uuid_or_none(value: object) -> UUID | None:
    return UUID(str(value)) if value else None


def _notification(event: OutboxEvent) -> Notification | None:
    user_id = UUID(str(event.payload["user_id"]))
    if event.event_type in _TRANSFER_TYPES:
        text = _transfer_text(event)
        if text is None:
            return None
        return Notification(
            user_id=user_id,
            type=event.event_type,
            title=text[0],
            body=text[1],
            resource_type="transaction",
            resource_id=_uuid_or_none(event.payload.get("transaction_id")),
            # events enqueued before #45 carry no account id: the link opens Accounts instead
            account_id=_uuid_or_none(event.payload.get("account_id")),
        )
    if event.event_type in _SECURITY_TEXT:
        title, body = _SECURITY_TEXT[event.event_type]
        return Notification(
            user_id=user_id,
            type=event.event_type,
            title=title,
            body=body,
            resource_type="security",
        )
    return None


def relay_outbox(session: Session, batch_size: int = 100) -> int:
    """Turn pending outbox events into notifications. Safe with several concurrent workers."""
    events = (
        session.execute(
            select(OutboxEvent)
            .where(OutboxEvent.processed_at.is_(None))
            .order_by(OutboxEvent.created_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    for event in events:
        notification = _notification(event)
        if notification is not None:
            session.add(notification)
        event.processed_at = utcnow()
    session.flush()
    return len(events)


def list_notifications(
    session: Session, user_id: UUID, unread_only: bool, limit: int, offset: int = 0
) -> tuple[list[Notification], int, int]:
    """Fetch notifications for a user with pagination and total/unread counts."""
    base_stmt = select(Notification).where(Notification.user_id == user_id)
    filtered_stmt = base_stmt.where(Notification.read_at.is_(None)) if unread_only else base_stmt

    total = session.execute(select(func.count()).select_from(filtered_stmt.subquery())).scalar_one()

    unread_count = session.execute(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
    ).scalar_one()

    items_stmt = (
        filtered_stmt.order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset(offset)
        .limit(limit)
    )
    items = list(session.execute(items_stmt).scalars().all())

    return items, total, unread_count


def mark_read(session: Session, user_id: UUID, notification_id: UUID) -> Notification:
    """Mark a single notification as read if owned by user_id, returning the notification."""
    notification = get_owned_notification(session, user_id, notification_id)
    if notification.read_at is None:
        notification.read_at = utcnow()
        session.commit()
    return notification


def mark_all_read(session: Session, user_id: UUID) -> int:
    """Bulk mark all unread notifications belonging to user_id as read."""
    now = utcnow()
    result = session.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=now)
    )
    # rowcount is missing from SQLAlchemy's CursorResult stubs; for an UPDATE it is the row count.
    count = int(result.rowcount)  # type: ignore[attr-defined]
    session.commit()
    return count
