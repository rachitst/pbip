"""Transactional outbox + in-app notifications."""

from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from pbip_api.core.db import utcnow
from pbip_api.core.errors import NotFoundError
from pbip_api.core.money import format_minor
from pbip_api.modules.notifications.models import Notification, OutboxEvent

TRANSFER_POSTED = "TRANSFER_POSTED"
TRANSFER_DECLINED = "TRANSFER_DECLINED"
TRANSFER_UNDER_REVIEW = "TRANSFER_UNDER_REVIEW"


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
    session: Session, user_id: UUID, unread_only: bool, limit: int
) -> list[Notification]:
    stmt = (
        select(Notification)
        .where(Notification.user_id == user_id)
        .order_by(Notification.created_at.desc())
        .limit(limit)
    )
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    return list(session.execute(stmt).scalars().all())


def mark_read(session: Session, user_id: UUID, notification_id: UUID) -> None:
    result = session.execute(
        update(Notification)
        .where(Notification.id == notification_id, Notification.user_id == user_id)
        .values(read_at=utcnow())
    )
    if result.rowcount == 0:  # type: ignore[attr-defined]
        raise NotFoundError("notification not found")
    session.commit()
