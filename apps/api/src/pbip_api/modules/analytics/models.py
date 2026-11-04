"""Analytics models: pre-aggregated monthly spending and category totals."""

from datetime import datetime
from uuid import UUID, uuid4

from pbip_ml.recommendations import ProductType
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
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from pbip_api.core.db import Base, created_at_col, in_check, utcnow, uuid_pk


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

    # Migrations 0003/0004 gave this PK no server default; ids always come from Python.
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    month: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    total_amount_minor: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    transaction_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )


class CustomerSegment(Base):
    """A customer's behavioural segment, written only by the offline K-Means job (ADR-0012).

    The request path reads this table; it never assigns a segment itself. The RFM features the
    label was derived from are stored next to it so the assignment is explainable.
    """

    __tablename__ = "customer_segments"
    __table_args__ = (
        UniqueConstraint("customer_id", name="uq_customer_segments_customer_id"),
        Index("ix_customer_segments_segment", "segment"),
        CheckConstraint(
            "segment IN ('HIGH_NET_WORTH', 'BUDGET_CONSCIOUS', 'FREQUENT_TRANSACTOR', "
            "'DISCRETIONARY_SPENDER', 'INACTIVE_SAVER')",
            name="segment_valid",
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

    # Migrations 0003/0004 gave this PK no server default; ids always come from Python.
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
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
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )


class CustomerBehaviourProfile(Base):
    """A consenting customer's engagement over the last ``window_days`` (UC-11, ADR-0057).

    Written only by the daily profile job; deleted with the customer, and at once when the
    customer withdraws analytics consent. Segment, spend and income are not copied here: the
    profile API reads them from their own sources so they are never stale twice.
    """

    __tablename__ = "customer_behaviour_profiles"
    __table_args__ = (
        UniqueConstraint("customer_id"),
        CheckConstraint("window_days BETWEEN 1 AND 180", name="window_days_range"),
        CheckConstraint("sessions >= 0", name="sessions_non_negative"),
        CheckConstraint("avg_session_seconds >= 0", name="avg_session_seconds_non_negative"),
        CheckConstraint("interactions >= sessions", name="interactions_cover_sessions"),
        CheckConstraint("page_views >= 0 AND page_views <= interactions", name="page_views_range"),
        CheckConstraint(
            "(sessions = 0) = (last_active_at IS NULL)", name="last_active_iff_sessions"
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    customer_id: Mapped[UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    window_days: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_session_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    page_views: Mapped[int] = mapped_column(Integer, nullable=False)
    interactions: Mapped[int] = mapped_column(Integer, nullable=False)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )


class CustomerProductInterest(Base):
    """One product category a consenting customer showed interest in (UC-11, ADR-0057).

    Replaced for the customer on every profile job run; ``score`` is the recency-weighted sum of
    the signals (``profiling.score_interests``), so only categories with a signal have a row.
    """

    __tablename__ = "customer_product_interests"
    __table_args__ = (
        UniqueConstraint("customer_id", "category"),
        Index("ix_customer_product_interests_category", "category"),
        in_check("category", ProductType, "category_valid"),
        CheckConstraint("score >= 0.0", name="score_non_negative"),
        CheckConstraint("signals >= 1", name="signals_positive"),
    )

    id: Mapped[UUID] = uuid_pk()
    customer_id: Mapped[UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    signals: Mapped[int] = mapped_column(Integer, nullable=False)
    last_signal_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = created_at_col()
