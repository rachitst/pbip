"""Accounts and account holders.

``ledger_balance`` and ``held_amount`` are a MATERIALISED view of the immutable ledger
(transaction_entries). They are only ever changed by the ledger module, inside the same
database transaction that inserts the entries, under a row lock.
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from pbip_api.core.db import Base, created_at_col, in_check, str_enum, uuid_pk


class AccountType(StrEnum):
    SAVINGS = "SAVINGS"
    CURRENT = "CURRENT"
    SYSTEM = "SYSTEM"  # internal general-ledger accounts (clearing, fees); may go negative


class AccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    FROZEN = "FROZEN"
    CLOSED = "CLOSED"


class HolderRole(StrEnum):
    PRIMARY = "PRIMARY"
    JOINT = "JOINT"


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        in_check("account_type", AccountType, "type_valid"),
        in_check("status", AccountStatus, "status_valid"),
        CheckConstraint("held_amount >= 0", name="held_non_negative"),
        CheckConstraint("overdraft_limit >= 0", name="overdraft_non_negative"),
        # Last line of defence: even a buggy service cannot overdraw a customer account.
        CheckConstraint(
            "account_type = 'SYSTEM' OR (ledger_balance - held_amount + overdraft_limit) >= 0",
            name="available_non_negative",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    account_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    account_type: Mapped[AccountType] = mapped_column(str_enum(AccountType), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    status: Mapped[AccountStatus] = mapped_column(
        str_enum(AccountStatus), default=AccountStatus.ACTIVE, nullable=False
    )
    ledger_balance: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    held_amount: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    overdraft_limit: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = created_at_col()

    @property
    def available_balance(self) -> int:
        return self.ledger_balance - self.held_amount

    @property
    def spendable(self) -> int:
        return self.ledger_balance - self.held_amount + self.overdraft_limit


class AccountHolder(Base):
    __tablename__ = "account_holders"
    __table_args__ = (
        in_check("holder_role", HolderRole, "holder_role_valid"),
        Index("ix_account_holders_customer_id", "customer_id"),
    )

    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    customer_id: Mapped[UUID] = mapped_column(ForeignKey("customers.id"), primary_key=True)
    holder_role: Mapped[HolderRole] = mapped_column(
        str_enum(HolderRole), default=HolderRole.PRIMARY, nullable=False
    )
