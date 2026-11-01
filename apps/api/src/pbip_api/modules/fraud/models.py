"""Fraud events (every scoring decision, for audit/retraining) and analyst alerts."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pbip_api.core.db import Base, created_at_col, in_check, str_enum, uuid_pk


class AlertStatus(StrEnum):
    OPEN = "OPEN"
    CONFIRMED_FRAUD = "CONFIRMED_FRAUD"
    FALSE_POSITIVE = "FALSE_POSITIVE"


class AlertSeverity(StrEnum):
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class FraudEvent(Base):
    __tablename__ = "fraud_events"
    __table_args__ = (
        Index("ix_fraud_events_transaction_id", "transaction_id"),
        Index("ix_fraud_events_decision_created_at", "decision", "created_at"),
    )

    id: Mapped[UUID] = uuid_pk()
    transaction_id: Mapped[UUID] = mapped_column(ForeignKey("transactions.id"), nullable=False)
    customer_id: Mapped[UUID] = mapped_column(ForeignKey("customers.id"), nullable=False)
    risk_score: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    features: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    model_version: Mapped[str] = mapped_column(String(80), nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class FraudAlert(Base):
    __tablename__ = "fraud_alerts"
    __table_args__ = (
        in_check("status", AlertStatus, "status_valid"),
        in_check("severity", AlertSeverity, "severity_valid"),
        Index("ix_fraud_alerts_status_created_at", "status", "created_at"),
        # FK, and the alert queue's transaction_id filter (the demo lab finds its own alert)
        Index("ix_fraud_alerts_transaction_id", "transaction_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    fraud_event_id: Mapped[UUID] = mapped_column(
        ForeignKey("fraud_events.id"), unique=True, nullable=False
    )
    transaction_id: Mapped[UUID] = mapped_column(ForeignKey("transactions.id"), nullable=False)
    status: Mapped[AlertStatus] = mapped_column(
        str_enum(AlertStatus), default=AlertStatus.OPEN, nullable=False
    )
    severity: Mapped[AlertSeverity] = mapped_column(str_enum(AlertSeverity), nullable=False)
    assigned_to_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
