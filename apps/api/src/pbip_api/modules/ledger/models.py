"""Double-entry ledger: transactions (headers), entries (immutable), holds, idempotency keys."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pbip_api.core.db import Base, created_at_col, in_check, str_enum, uuid_pk


class TransactionType(StrEnum):
    TRANSFER = "TRANSFER"
    DEPOSIT = "DEPOSIT"
    REVERSAL = "REVERSAL"
    FEE = "FEE"


class TransactionStatus(StrEnum):
    INITIATED = "INITIATED"  # transient inside one DB transaction; never visible after commit
    PENDING_STEP_UP = "PENDING_STEP_UP"  # waiting for the customer's second factor
    PENDING_REVIEW = "PENDING_REVIEW"  # funds held, waiting for a fraud analyst
    POSTED = "POSTED"  # entries written; final
    DECLINED = "DECLINED"  # rejected (fraud block / analyst reject); no entries
    REVERSED = "REVERSED"  # a compensating REVERSAL transaction exists


class EntryDirection(StrEnum):
    DEBIT = "DEBIT"  # decreases the account balance
    CREDIT = "CREDIT"  # increases the account balance


class HoldStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    CAPTURED = "CAPTURED"


class Transaction(Base):
    """Payment instruction + lifecycle. The ENTRIES are the financial truth."""

    __tablename__ = "transactions"
    __table_args__ = (
        in_check("type", TransactionType, "type_valid"),
        in_check("status", TransactionStatus, "status_valid"),
        CheckConstraint("amount > 0", name="amount_positive"),
        Index(
            "ix_transactions_initiated_by_user_id_created_at", "initiated_by_user_id", "created_at"
        ),
        Index("ix_transactions_debit_account_id_created_at", "debit_account_id", "created_at"),
        Index("ix_transactions_status", "status"),
    )

    id: Mapped[UUID] = uuid_pk()
    type: Mapped[TransactionType] = mapped_column(str_enum(TransactionType), nullable=False)
    status: Mapped[TransactionStatus] = mapped_column(str_enum(TransactionStatus), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    initiated_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    debit_account_id: Mapped[UUID | None] = mapped_column(ForeignKey("accounts.id"))
    credit_account_id: Mapped[UUID | None] = mapped_column(ForeignKey("accounts.id"))
    payee_id: Mapped[UUID | None] = mapped_column(ForeignKey("payees.id"))
    reference: Mapped[str | None] = mapped_column(String(140))
    decline_reason: Mapped[str | None] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(String(10), default="WEB", nullable=False)
    reversal_of_id: Mapped[UUID | None] = mapped_column(ForeignKey("transactions.id"))
    created_at: Mapped[datetime] = created_at_col()
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TransactionEntry(Base):
    """IMMUTABLE ledger line. Enforced by database triggers (see the core-schema migration)."""

    __tablename__ = "transaction_entries"
    __table_args__ = (
        in_check("direction", EntryDirection, "direction_valid"),
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_transaction_entries_account_id_seq", "account_id", "seq"),
        Index("ix_transaction_entries_transaction_id", "transaction_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True, nullable=False)
    transaction_id: Mapped[UUID] = mapped_column(ForeignKey("transactions.id"), nullable=False)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    direction: Mapped[EntryDirection] = mapped_column(str_enum(EntryDirection), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class Hold(Base):
    """Funds reserved for a transaction awaiting review. Reduces available, not ledger, balance."""

    __tablename__ = "holds"
    __table_args__ = (
        in_check("status", HoldStatus, "status_valid"),
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_holds_account_id", "account_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    transaction_id: Mapped[UUID] = mapped_column(
        ForeignKey("transactions.id"), unique=True, nullable=False
    )
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[HoldStatus] = mapped_column(
        str_enum(HoldStatus), default=HoldStatus.ACTIVE, nullable=False
    )
    created_at: Mapped[datetime] = created_at_col()
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IdempotencyKey(Base):
    """One row per (user, Idempotency-Key). The UNIQUE constraint is what makes retries safe."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_idempotency_keys_user_key"),)

    id: Mapped[UUID] = uuid_pk()
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    transaction_id: Mapped[UUID | None] = mapped_column(ForeignKey("transactions.id"))
    created_at: Mapped[datetime] = created_at_col()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
