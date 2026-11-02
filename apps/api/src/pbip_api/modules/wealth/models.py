"""Wealth catalogue and fixed deposits (TASK-063, TASK-064, ADR-0025).

``wealth_products`` is reference data seeded by migration 0018 (identical in every environment; a
rate change is a new migration). A fixed deposit stores a SNAPSHOT of the terms it was booked on, so
a later catalogue change never rewrites an existing contract. "Matured" is derived (ACTIVE and the
maturity date reached), never stored, so it cannot go stale.
"""

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from pbip_api.core.db import Base, created_at_col, in_check, str_enum, uuid_pk


class ProductType(StrEnum):
    FIXED_DEPOSIT = "FIXED_DEPOSIT"
    MUTUAL_FUND = "MUTUAL_FUND"
    SOVEREIGN_GOLD_BOND = "SOVEREIGN_GOLD_BOND"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


class YieldBasis(StrEnum):
    GUARANTEED = "GUARANTEED"  # contract rate, compounded quarterly
    FIXED_COUPON = "FIXED_COUPON"  # coupon paid out; the price of the asset also moves
    MARKET_LINKED = "MARKET_LINKED"  # no yield is stated: PBIP has no market data


class FixedDepositStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


class ClosureKind(StrEnum):
    MATURED = "MATURED"  # closed on or after the maturity date: the projected amount is paid
    PREMATURE = "PREMATURE"  # closed early: penalty rate for the time held


class WealthProduct(Base):
    __tablename__ = "wealth_products"
    __table_args__ = (
        in_check("product_type", ProductType, "product_type_valid"),
        in_check("risk_level", RiskLevel, "risk_level_valid"),
        in_check("yield_basis", YieldBasis, "yield_basis_valid"),
        CheckConstraint("annual_rate_bps IS NULL OR annual_rate_bps >= 0", name="rate_valid"),
        CheckConstraint(
            "(yield_basis = 'MARKET_LINKED') = (annual_rate_bps IS NULL)",
            name="rate_only_when_not_market_linked",
        ),
        CheckConstraint("min_lock_in_months >= 0", name="lock_in_non_negative"),
        CheckConstraint(
            "min_amount_minor IS NULL OR min_amount_minor > 0", name="min_amount_positive"
        ),
        CheckConstraint(
            "max_amount_minor IS NULL OR max_amount_minor >= min_amount_minor",
            name="max_amount_at_least_min",
        ),
        CheckConstraint(
            "premature_penalty_bps IS NULL OR premature_penalty_bps >= 0",
            name="penalty_non_negative",
        ),
        CheckConstraint(
            "NOT bookable OR (product_type = 'FIXED_DEPOSIT' AND annual_rate_bps IS NOT NULL "
            "AND min_amount_minor IS NOT NULL AND max_amount_minor IS NOT NULL "
            "AND cardinality(tenures_months) > 0)",
            name="bookable_is_a_complete_deposit",
        ),
        Index("ix_wealth_products_risk_level", "risk_level"),
    )

    id: Mapped[UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    product_type: Mapped[ProductType] = mapped_column(str_enum(ProductType), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    risk_level: Mapped[RiskLevel] = mapped_column(str_enum(RiskLevel), nullable=False)
    yield_basis: Mapped[YieldBasis] = mapped_column(str_enum(YieldBasis), nullable=False)
    annual_rate_bps: Mapped[int | None] = mapped_column(Integer)
    tenures_months: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False)
    min_lock_in_months: Mapped[int] = mapped_column(Integer, nullable=False)
    min_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    max_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    premature_penalty_bps: Mapped[int | None] = mapped_column(Integer)
    bookable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    notes: Mapped[str] = mapped_column(String(300), nullable=False)
    catalogue_version: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class FixedDeposit(Base):
    __tablename__ = "fixed_deposits"
    __table_args__ = (
        in_check("status", FixedDepositStatus, "status_valid"),
        in_check("closure_kind", ClosureKind, "closure_kind_valid"),
        CheckConstraint("principal > 0", name="principal_positive"),
        CheckConstraint("annual_rate_bps >= 0", name="rate_non_negative"),
        CheckConstraint(
            "tenure_months > 0 AND mod(tenure_months, 3) = 0", name="tenure_in_quarters"
        ),
        CheckConstraint("lock_in_months >= 0", name="lock_in_non_negative"),
        CheckConstraint("maturity_interest >= 0", name="maturity_interest_non_negative"),
        CheckConstraint(
            "maturity_amount = principal + maturity_interest",
            name="maturity_amount_is_principal_plus_interest",
        ),
        CheckConstraint("maturity_date > start_date", name="matures_after_start"),
        CheckConstraint(
            "premature_penalty_bps IS NULL OR premature_penalty_bps >= 0",
            name="penalty_non_negative",
        ),
        CheckConstraint(
            "(status = 'CLOSED') = (closed_at IS NOT NULL AND closed_on IS NOT NULL "
            "AND closure_kind IS NOT NULL AND closure_transaction_id IS NOT NULL "
            "AND paid_interest IS NOT NULL)",
            name="closed_has_payout",
        ),
        CheckConstraint(
            "paid_interest IS NULL OR paid_interest >= 0", name="paid_interest_non_negative"
        ),
        CheckConstraint(
            "interest_transaction_id IS NULL OR paid_interest > 0",
            name="interest_transaction_only_for_interest",
        ),
        Index("ix_fixed_deposits_customer_id_created_at", "customer_id", "created_at"),
        Index("ix_fixed_deposits_user_id", "user_id"),
        Index("ix_fixed_deposits_account_id", "account_id"),
        Index("ix_fixed_deposits_product_id", "product_id"),
        # ADR-0029: a payout can only be held while the deposit is still open.
        CheckConstraint(
            "payout_held_at IS NULL OR status = 'ACTIVE'", name="payout_held_only_while_open"
        ),
        # ADR-0029: the hourly maturity payout poll (status = ACTIVE AND maturity_date <= today).
        Index("ix_fixed_deposits_status_maturity_date", "status", "maturity_date"),
    )

    id: Mapped[UUID] = uuid_pk()
    customer_id: Mapped[UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )  # funded the deposit and receives the payout
    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("wealth_products.id", ondelete="RESTRICT"), nullable=False
    )
    booking_transaction_id: Mapped[UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    closure_transaction_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT"), unique=True
    )
    interest_transaction_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT"), unique=True
    )
    annual_rate_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    tenure_months: Mapped[int] = mapped_column(Integer, nullable=False)
    lock_in_months: Mapped[int] = mapped_column(Integer, nullable=False)
    premature_penalty_bps: Mapped[int | None] = mapped_column(Integer)
    principal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    maturity_interest: Mapped[int] = mapped_column(BigInteger, nullable=False)
    maturity_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    maturity_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[FixedDepositStatus] = mapped_column(
        str_enum(FixedDepositStatus), default=FixedDepositStatus.ACTIVE, nullable=False
    )
    closure_kind: Mapped[ClosureKind | None] = mapped_column(str_enum(ClosureKind))
    paid_interest: Mapped[int | None] = mapped_column(BigInteger)
    closed_on: Mapped[date | None] = mapped_column(Date)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the automatic maturity payout first found the account not active (ADR-0029); the
    # customer is notified once per hold, and the payout is retried every run until it succeeds.
    payout_held_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
