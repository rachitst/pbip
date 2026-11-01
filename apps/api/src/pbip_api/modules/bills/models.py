"""Models for registered utility billers and customer bill payments."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from pbip_api.core.db import Base, created_at_col, in_check, str_enum, uuid_pk


class BillerCategory(StrEnum):
    ELECTRICITY = "ELECTRICITY"
    WATER = "WATER"
    TELECOM = "TELECOM"
    GAS = "GAS"
    BROADBAND = "BROADBAND"
    INSURANCE = "INSURANCE"


class BillerStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class BillPaymentStatus(StrEnum):
    POSTED = "POSTED"
    PENDING_REVIEW = "PENDING_REVIEW"  # funds held, waiting for a fraud analyst (like transfers)
    DECLINED = "DECLINED"
    REVERSED = "REVERSED"


class Biller(Base):
    """Registered utility biller that customers can pay through the clearing account."""

    __tablename__ = "billers"
    __table_args__ = (
        in_check("category", BillerCategory, "category_valid"),
        in_check("status", BillerStatus, "status_valid"),
        Index("ix_billers_status", "status"),
    )

    id: Mapped[UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[BillerCategory] = mapped_column(str_enum(BillerCategory), nullable=False)
    biller_code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    status: Mapped[BillerStatus] = mapped_column(
        str_enum(BillerStatus), default=BillerStatus.ACTIVE, nullable=False
    )
    created_at: Mapped[datetime] = created_at_col()


class BillPayment(Base):
    """Customer bill payment instruction linked to an immutable double-entry ledger transaction."""

    __tablename__ = "bill_payments"
    __table_args__ = (
        in_check("status", BillPaymentStatus, "status_valid"),
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_bill_payments_customer_id_created_at", "customer_id", "created_at"),
        Index("ix_bill_payments_biller_id", "biller_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    customer_id: Mapped[UUID] = mapped_column(ForeignKey("customers.id"), nullable=False)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    biller_id: Mapped[UUID] = mapped_column(ForeignKey("billers.id"), nullable=False)
    transaction_id: Mapped[UUID] = mapped_column(
        ForeignKey("transactions.id"), unique=True, nullable=False
    )
    consumer_reference: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[BillPaymentStatus] = mapped_column(
        str_enum(BillPaymentStatus), default=BillPaymentStatus.POSTED, nullable=False
    )
    created_at: Mapped[datetime] = created_at_col()
