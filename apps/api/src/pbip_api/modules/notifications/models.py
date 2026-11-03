"""In-app notifications and the transactional outbox that feeds them."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pbip_api.core.db import Base, created_at_col, uuid_pk


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_id_created_at", "user_id", "created_at"),
        CheckConstraint(
            "resource_type IS NULL OR resource_type IN ('transaction', 'security')",
            name="resource_type_valid",
        ),
        # Exactly-once delivery (TASK-026, ADR-0040): one notification per outbox event, enforced
        # by the database, so a re-delivered event can never notify twice.
        UniqueConstraint("outbox_event_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    title: Mapped[str] = mapped_column(String(140), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Optional link target: a transfer notification points at its transaction (and the sender's
    # account, so the web app can open that account's transactions); security ones at "security".
    resource_type: Mapped[str | None] = mapped_column(String(20))
    resource_id: Mapped[UUID | None] = mapped_column()
    account_id: Mapped[UUID | None] = mapped_column()
    # The event this notification was delivered for; NULL for ones written directly (TASK-043).
    # No FK: processed events are purged after the retention period (ADR-0040), the notification
    # stays. It is only the de-duplication key.
    outbox_event_id: Mapped[UUID | None] = mapped_column()
    created_at: Mapped[datetime] = created_at_col()


class OutboxEvent(Base):
    """Written in the SAME transaction as the business change, relayed asynchronously.

    Guarantees a notification is produced if and only if the business change committed. A failed
    delivery is retried with exponential backoff (``attempts``, ``next_attempt_at``) and, after the
    last attempt, dead-lettered (``dead_lettered_at``); processed events are purged after the
    retention period (TASK-026, ADR-0040).
    """

    __tablename__ = "outbox_events"
    __table_args__ = (
        # The relay's poll: pending events that are due, oldest due first.
        Index(
            "ix_outbox_events_due",
            "next_attempt_at",
            postgresql_where=text("processed_at IS NULL AND dead_lettered_at IS NULL"),
        ),
        # The daily purge of processed events older than the retention period.
        Index(
            "ix_outbox_events_processed_at",
            "processed_at",
            postgresql_where=text("processed_at IS NOT NULL"),
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint(
            "processed_at IS NULL OR dead_lettered_at IS NULL", name="processed_or_dead_lettered"
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Failed deliveries so far; the relay skips the event until ``next_attempt_at``.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # The exception class of the last failure only: a message could carry payload data.
    last_error: Mapped[str | None] = mapped_column(String(100))
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
