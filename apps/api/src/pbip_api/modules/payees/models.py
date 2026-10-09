"""Saved payees. ``created_at`` is a fraud signal (very new payee + large amount)."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from pbip_api.core.db import Base, created_at_col, in_check, str_enum, uuid_pk


class PayeeStatus(StrEnum):
    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    VERIFIED = "VERIFIED"
    BLOCKED = "BLOCKED"


class Payee(Base):
    __tablename__ = "payees"
    __table_args__ = (
        UniqueConstraint(
            "customer_id", "bank_code", "account_number", name="uq_payees_customer_destination"
        ),
        Index("ix_payees_customer_id", "customer_id"),
        in_check("status", PayeeStatus, "status_valid"),
    )

    id: Mapped[UUID] = uuid_pk()
    customer_id: Mapped[UUID] = mapped_column(ForeignKey("customers.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    account_number: Mapped[str] = mapped_column(String(20), nullable=False)
    bank_code: Mapped[str] = mapped_column(String(20), nullable=False)
    nickname: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[PayeeStatus] = mapped_column(
        str_enum(PayeeStatus), default=PayeeStatus.PENDING_VERIFICATION, nullable=False
    )
    created_at: Mapped[datetime] = created_at_col()
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
