"""Loans and their repayment schedules (TASK-047, ADR-0024).

A loan stores a SNAPSHOT of the catalogue terms it was approved on (rate, tenure, EMI), so a later
catalogue change never rewrites an existing contract. The instalments are ``emi.schedule()`` rows
verbatim. "Overdue" is derived (DUE and past its due date), never stored, so it cannot go stale.
"""

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from pbip_api.core.db import Base, created_at_col, in_check, str_enum, uuid_pk
from pbip_api.modules.loans.catalogue import LoanType


class LoanStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"  # every instalment paid


class InstalmentStatus(StrEnum):
    DUE = "DUE"
    PAID = "PAID"


class Loan(Base):
    __tablename__ = "loans"
    __table_args__ = (
        in_check("loan_type", LoanType, "loan_type_valid"),
        in_check("status", LoanStatus, "status_valid"),
        CheckConstraint("principal > 0", name="principal_positive"),
        CheckConstraint("emi > 0", name="emi_positive"),
        CheckConstraint("tenure_months > 0", name="tenure_positive"),
        CheckConstraint("annual_rate_bps >= 0", name="rate_non_negative"),
        CheckConstraint("total_interest >= 0", name="total_interest_non_negative"),
        CheckConstraint(
            "(status = 'CLOSED') = (closed_at IS NOT NULL)", name="closed_at_matches_status"
        ),
        Index("ix_loans_customer_id_created_at", "customer_id", "created_at"),
        Index("ix_loans_user_id", "user_id"),
        Index("ix_loans_account_id", "account_id"),
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
    )
    disbursal_transaction_id: Mapped[UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    product_id: Mapped[UUID] = mapped_column(nullable=False)  # catalogue lives in code: no FK
    loan_type: Mapped[LoanType] = mapped_column(str_enum(LoanType), nullable=False)
    annual_rate_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    tenure_months: Mapped[int] = mapped_column(Integer, nullable=False)
    principal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    emi: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_interest: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[LoanStatus] = mapped_column(
        str_enum(LoanStatus), default=LoanStatus.ACTIVE, nullable=False
    )
    created_at: Mapped[datetime] = created_at_col()
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoanInstalment(Base):
    __tablename__ = "loan_instalments"
    __table_args__ = (
        in_check("status", InstalmentStatus, "status_valid"),
        UniqueConstraint("loan_id", "seq"),
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("principal >= 0 AND interest >= 0", name="parts_non_negative"),
        CheckConstraint("amount = principal + interest", name="amount_is_principal_plus_interest"),
        CheckConstraint("balance_after >= 0", name="balance_after_non_negative"),
        CheckConstraint(
            "(status = 'PAID') = (transaction_id IS NOT NULL AND paid_at IS NOT NULL)",
            name="paid_has_transaction",
        ),
        Index("ix_loan_instalments_transaction_id", "transaction_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    loan_id: Mapped[UUID] = mapped_column(
        ForeignKey("loans.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    principal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    interest: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[InstalmentStatus] = mapped_column(
        str_enum(InstalmentStatus), default=InstalmentStatus.DUE, nullable=False
    )
    transaction_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT")
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
