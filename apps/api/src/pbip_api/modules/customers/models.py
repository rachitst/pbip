"""Customer profile and known devices."""

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pbip_api.core.db import Base, created_at_col, in_check, str_enum, uuid_pk
from pbip_api.modules.customers.risk import MAX_SCORE, RiskBand


class KycStatus(StrEnum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        in_check("kyc_status", KycStatus, "kyc_status_valid"),
        in_check("risk_profile", RiskBand, "risk_profile_valid"),
        CheckConstraint(f"risk_score BETWEEN 0 AND {MAX_SCORE}", name="risk_score_range"),
        # A band only exists together with the answers, score and time that produced it.
        CheckConstraint(
            "(risk_profile IS NULL AND risk_score IS NULL AND risk_answers IS NULL"
            " AND risk_questionnaire_version IS NULL AND risk_assessed_at IS NULL)"
            " OR (risk_profile IS NOT NULL AND risk_score IS NOT NULL AND risk_answers IS NOT NULL"
            " AND risk_questionnaire_version IS NOT NULL AND risk_assessed_at IS NOT NULL)",
            name="risk_assessment_complete",
        ),
        # Consent is evidence: a grant always says when and under which policy version.
        CheckConstraint(
            "(recommendations_consent_at IS NULL) = (recommendations_consent_policy IS NULL)",
            name="recommendations_consent_complete",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Self-declared, optional, never inferred (ADR-0015); readable only by the customer and the
    # recommendation service, which turns it into an age band and only with consent.
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    phone_enc: Mapped[str | None] = mapped_column(Text)  # field-level encrypted
    kyc_status: Mapped[KycStatus] = mapped_column(
        str_enum(KycStatus), default=KycStatus.PENDING, nullable=False
    )
    segment: Mapped[str | None] = mapped_column(String(40))  # set by the segmentation job
    # Risk tolerance (TASK-097): the band, plus the answers and score that produced it.
    risk_profile: Mapped[str | None] = mapped_column(String(20))
    risk_score: Mapped[int | None] = mapped_column(SmallInteger)
    risk_answers: Mapped[dict[str, str] | None] = mapped_column(JSONB)
    risk_questionnaire_version: Mapped[str | None] = mapped_column(String(20))
    risk_assessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ADR-0015 consent to use the date of birth for recommendations; NULL = not granted.
    recommendations_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recommendations_consent_policy: Mapped[str | None] = mapped_column(String(40))
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
