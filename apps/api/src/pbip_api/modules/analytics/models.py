"""Analytics models: pre-aggregated monthly spending and customer behavioral segments."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from pbip_api.core.db import Base, created_at_col, utcnow, uuid_pk


class MonthlyCategorySpending(Base):
    """Pre-aggregated monthly customer spending per category.

    Materialized table updated periodically or on demand to maintain
    sub-50ms query response times for spending breakdown and trends.
    """

    __tablename__ = "monthly_category_spending"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "year",
            "month",
            "category",
            name="uq_monthly_category_spending_account_period_cat",
        ),
        Index("ix_monthly_category_spending_account_period", "account_id", "year", "month"),
        CheckConstraint("year >= 2000", name="year_valid"),
        CheckConstraint("month BETWEEN 1 AND 12", name="month_valid"),
        CheckConstraint("total_amount_minor >= 0", name="total_amount_minor_non_negative"),
        CheckConstraint("transaction_count >= 0", name="transaction_count_non_negative"),
    )

    id: Mapped[UUID] = uuid_pk()
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    month: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    total_amount_minor: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    transaction_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class CustomerSegment(Base):
    """Customer behavioral segment assignment persisted from the ML clustering pipeline."""

    __tablename__ = "customer_segments"
    __table_args__ = (
        UniqueConstraint("customer_id", name="uq_customer_segments_customer_id"),
        Index("ix_customer_segments_segment", "segment"),
        CheckConstraint(
            "segment IN ('HIGH_NET_WORTH', 'BUDGET_CONSCIOUS', 'FREQUENT_TRANSACTOR', "
            "'DISCRETIONARY_SPENDER', 'INACTIVE_SAVER')",
            name="customer_segment_valid",
        ),
        CheckConstraint(
            "confidence_score >= 0.0 AND confidence_score <= 1.0", name="confidence_score_range"
        ),
        CheckConstraint("recency_days >= 0", name="recency_days_non_negative"),
        CheckConstraint("frequency >= 0.0", name="frequency_non_negative"),
        CheckConstraint("monetary_minor >= 0", name="monetary_minor_non_negative"),
        CheckConstraint(
            "discretionary_ratio >= 0.0 AND discretionary_ratio <= 1.0",
            name="discretionary_ratio_range",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    customer_id: Mapped[UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    segment: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    recency_days: Mapped[int] = mapped_column(Integer, nullable=False)
    frequency: Mapped[float] = mapped_column(Float, nullable=False)
    monetary_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    savings_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    discretionary_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    explanation: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
