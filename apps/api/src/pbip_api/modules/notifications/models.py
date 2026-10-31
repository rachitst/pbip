"""In-app notifications and the transactional outbox that feeds them."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
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
    created_at: Mapped[datetime] = created_at_col()


class OutboxEvent(Base):
    """Written in the SAME transaction as the business change, relayed asynchronously.

    Guarantees a notification is produced if and only if the business change committed.
    """

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index(
            "ix_outbox_events_unprocessed",
            "created_at",
            postgresql_where=text("processed_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
