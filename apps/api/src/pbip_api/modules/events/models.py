"""Clickstream event model for telemetry ingestion (TASK-055)."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pbip_api.core.db import Base, created_at_col


class ClickstreamEvent(Base):
    """Telemetry and clickstream events partitioned monthly by event_timestamp.

    Stores client-side and server-side user interaction telemetry (page views,
    calculator usage, flow initiations) for behavioral analytics and ML features.
    """

    __tablename__ = "clickstream_events"
    __table_args__ = (
        PrimaryKeyConstraint("id", "event_timestamp", name="pk_clickstream_events"),
        Index("ix_clickstream_events_customer_timestamp", "customer_id", "event_timestamp"),
        Index("ix_clickstream_events_session_timestamp", "session_id", "event_timestamp"),
        Index("ix_clickstream_events_event_type_timestamp", "event_type", "event_timestamp"),
        {"postgresql_partition_by": "RANGE (event_timestamp)"},
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    page_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    properties: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = created_at_col()
