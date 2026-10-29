"""Analytics models: pre-aggregated monthly spending and category totals."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
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
    """Per-category spend of one customer account in one calendar month.

    Written only by the nightly Celery task (``pbip.analytics.materialize_monthly_spending``,
    ADR-0011) for closed months; the request path reads it so a closed month costs one indexed
    lookup instead of a scan of the ledger entries.
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
