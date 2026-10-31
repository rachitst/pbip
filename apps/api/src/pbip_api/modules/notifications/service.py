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


def enqueue(session: Session, event_type: str, payload: dict[str, Any]) -> None:
    """Add an outbox event in the CALLER'S transaction (no PII beyond ids and amounts)."""
    session.add(OutboxEvent(event_type=event_type, payload=payload))


def _render(event: OutboxEvent) -> tuple[str, str] | None:
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
    if event.event_type == BILL_PAYMENT_POSTED:
        biller_name = event.payload.get("biller_name")
        if isinstance(biller_name, str) and biller_name:
            return (
                "Bill payment completed",
                f"Your bill payment of {amount} to {biller_name} was completed.",
            )
        return "Bill payment completed", f"Your bill payment of {amount} was completed."
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
        rendered = _render(event)
        if rendered is not None:
            title, body = rendered
            session.add(
                Notification(
                    user_id=UUID(str(event.payload["user_id"])),
                    type=event.event_type,
                    title=title,
                    body=body,
                )
            )
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
