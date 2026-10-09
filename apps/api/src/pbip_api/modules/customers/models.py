"""Customer profile and known devices."""

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from pbip_api.core.db import Base, created_at_col, in_check, str_enum, uuid_pk


class KycStatus(StrEnum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (in_check("kyc_status", KycStatus, "kyc_status_valid"),)

    id: Mapped[UUID] = uuid_pk()
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    phone_enc: Mapped[str | None] = mapped_column(Text)  # field-level encrypted
    kyc_status: Mapped[KycStatus] = mapped_column(
        str_enum(KycStatus), default=KycStatus.PENDING, nullable=False
    )
    segment: Mapped[str | None] = mapped_column(String(40))  # set by the segmentation job
    risk_profile: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = created_at_col()


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("customer_id", "fingerprint_hash", name="uq_devices_customer_fingerprint"),
        Index("ix_devices_customer_id", "customer_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    customer_id: Mapped[UUID] = mapped_column(ForeignKey("customers.id"), nullable=False)
    fingerprint_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str | None] = mapped_column(String(100))
    first_seen_at: Mapped[datetime] = created_at_col()
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    trusted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
