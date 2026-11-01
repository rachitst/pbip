"""Scheduled and recurring transfer instructions (TASK-015 / ADR-0021)."""

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, Date, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from pbip_api.core.db import Base, created_at_col, in_check, str_enum, uuid_pk


class ScheduleFrequency(StrEnum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


class ScheduledTransferStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


class SchedulePauseReason(StrEnum):
    CUSTOMER = "CUSTOMER"
    FRAUD_BLOCK = "FRAUD_BLOCK"
    CUSTOMER_ACTION_REQUIRED = "CUSTOMER_ACTION_REQUIRED"


class ScheduledTransfer(Base):
    """Recurring transfer standing order executed by Celery Beat per occurrence (ADR-0021)."""

    __tablename__ = "scheduled_transfers"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("failure_count >= 0", name="failure_count_non_negative"),
        in_check("frequency", ScheduleFrequency, "frequency_valid"),
        in_check("status", ScheduledTransferStatus, "status_valid"),
        CheckConstraint(
            "(payee_id IS NOT NULL AND to_account_id IS NULL) "
            "OR (payee_id IS NULL AND to_account_id IS NOT NULL)",
            name="destination_xor",
        ),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="end_date_valid"),
        CheckConstraint(
            "(status = 'PAUSED' AND pause_reason IS NOT NULL AND pause_reason IN "
            "('CUSTOMER', 'FRAUD_BLOCK', 'CUSTOMER_ACTION_REQUIRED')) "
            "OR (status <> 'PAUSED' AND pause_reason IS NULL)",
            name="pause_reason_valid",
        ),
        Index("ix_scheduled_transfers_status_next_run_date", "status", "next_run_date"),
        Index("ix_scheduled_transfers_customer_id_created_at", "customer_id", "created_at"),
        Index("ix_scheduled_transfers_user_id", "user_id"),
        Index("ix_scheduled_transfers_from_account_id", "from_account_id"),
        Index("ix_scheduled_transfers_payee_id", "payee_id"),
        Index("ix_scheduled_transfers_to_account_id", "to_account_id"),
        Index("ix_scheduled_transfers_last_transaction_id", "last_transaction_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    customer_id: Mapped[UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    from_account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    payee_id: Mapped[UUID | None] = mapped_column(ForeignKey("payees.id", ondelete="RESTRICT"))
    to_account_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT")
    )
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    reference: Mapped[str | None] = mapped_column(String(140))
    frequency: Mapped[ScheduleFrequency] = mapped_column(
        str_enum(ScheduleFrequency), nullable=False
    )
    status: Mapped[ScheduledTransferStatus] = mapped_column(
        str_enum(ScheduledTransferStatus),
        default=ScheduledTransferStatus.ACTIVE,
        nullable=False,
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date)
    next_run_date: Mapped[date] = mapped_column(Date, nullable=False)
    last_run_date: Mapped[date | None] = mapped_column(Date)
    last_transaction_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL")
    )
    failure_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    last_failure_reason: Mapped[str | None] = mapped_column(String(64))
    last_failed_occurrence_date: Mapped[date | None] = mapped_column(Date)
    pause_reason: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = created_at_col()
