"""Loans, schedules, loan repayment instructions and loan ledger types (TASK-047, ADR-0024).

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-02 12:00:00.000000+00:00

The downgrade restores the narrower CHECK constraints. If loan transactions or loan instructions
exist, those restorations fail and the whole downgrade rolls back: ledger history is never deleted
to make a downgrade pass.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TYPES = "'TRANSFER', 'DEPOSIT', 'REVERSAL', 'FEE', 'BILL_PAYMENT'"


def _create_loans() -> None:
    op.create_table(
        "loans",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("disbursal_transaction_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column(
            "loan_type",
            sa.Enum("PERSONAL", "AUTO", "HOME", name="loantype", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("annual_rate_bps", sa.Integer(), nullable=False),
        sa.Column("tenure_months", sa.Integer(), nullable=False),
        sa.Column("principal", sa.BigInteger(), nullable=False),
        sa.Column("emi", sa.BigInteger(), nullable=False),
        sa.Column("total_interest", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "CLOSED", name="loanstatus", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "loan_type IN ('PERSONAL', 'AUTO', 'HOME')", name=op.f("ck_loans_loan_type_valid")
        ),
        sa.CheckConstraint("status IN ('ACTIVE', 'CLOSED')", name=op.f("ck_loans_status_valid")),
        sa.CheckConstraint("principal > 0", name=op.f("ck_loans_principal_positive")),
        sa.CheckConstraint("emi > 0", name=op.f("ck_loans_emi_positive")),
        sa.CheckConstraint("tenure_months > 0", name=op.f("ck_loans_tenure_positive")),
        sa.CheckConstraint("annual_rate_bps >= 0", name=op.f("ck_loans_rate_non_negative")),
        sa.CheckConstraint(
            "total_interest >= 0", name=op.f("ck_loans_total_interest_non_negative")
        ),
        sa.CheckConstraint(
            "(status = 'CLOSED') = (closed_at IS NOT NULL)",
            name=op.f("ck_loans_closed_at_matches_status"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_loans_customer_id_customers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_loans_user_id_users"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_loans_account_id_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["disbursal_transaction_id"],
            ["transactions.id"],
            name=op.f("fk_loans_disbursal_transaction_id_transactions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_loans")),
        sa.UniqueConstraint(
            "disbursal_transaction_id", name=op.f("uq_loans_disbursal_transaction_id")
        ),
    )
    op.create_index("ix_loans_customer_id_created_at", "loans", ["customer_id", "created_at"])
    op.create_index("ix_loans_user_id", "loans", ["user_id"])
    op.create_index("ix_loans_account_id", "loans", ["account_id"])


def _create_instalments() -> None:
    op.create_table(
        "loan_instalments",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("loan_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("principal", sa.BigInteger(), nullable=False),
        sa.Column("interest", sa.BigInteger(), nullable=False),
        sa.Column("balance_after", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("DUE", "PAID", name="instalmentstatus", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("transaction_id", sa.Uuid(), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('DUE', 'PAID')", name=op.f("ck_loan_instalments_status_valid")
        ),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_loan_instalments_seq_positive")),
        sa.CheckConstraint("amount > 0", name=op.f("ck_loan_instalments_amount_positive")),
        sa.CheckConstraint(
            "principal >= 0 AND interest >= 0",
            name=op.f("ck_loan_instalments_parts_non_negative"),
        ),
        sa.CheckConstraint(
            "amount = principal + interest",
            name=op.f("ck_loan_instalments_amount_is_principal_plus_interest"),
        ),
        sa.CheckConstraint(
            "balance_after >= 0", name=op.f("ck_loan_instalments_balance_after_non_negative")
        ),
        sa.CheckConstraint(
            "(status = 'PAID') = (transaction_id IS NOT NULL AND paid_at IS NOT NULL)",
            name=op.f("ck_loan_instalments_paid_has_transaction"),
        ),
        sa.ForeignKeyConstraint(
            ["loan_id"],
            ["loans.id"],
            name=op.f("fk_loan_instalments_loan_id_loans"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name=op.f("fk_loan_instalments_transaction_id_transactions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_loan_instalments")),
        sa.UniqueConstraint("loan_id", "seq", name=op.f("uq_loan_instalments_loan_id_seq")),
    )
    op.create_index("ix_loan_instalments_transaction_id", "loan_instalments", ["transaction_id"])


def upgrade() -> None:
    _create_loans()
    _create_instalments()

    op.add_column("scheduled_transfers", sa.Column("loan_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_scheduled_transfers_loan_id_loans"),
        "scheduled_transfers",
        "loans",
        ["loan_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        op.f("uq_scheduled_transfers_loan_id"), "scheduled_transfers", ["loan_id"]
    )
    op.drop_constraint(
        op.f("ck_scheduled_transfers_destination_xor"), "scheduled_transfers", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_scheduled_transfers_destination_xor"),
        "scheduled_transfers",
        "num_nonnulls(payee_id, to_account_id, loan_id) = 1",
    )

    op.drop_constraint(op.f("ck_transactions_type_valid"), "transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_transactions_type_valid"),
        "transactions",
        f"type IN ({_TYPES}, 'LOAN_DISBURSAL', 'LOAN_REPAYMENT')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_transactions_type_valid"), "transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_transactions_type_valid"), "transactions", f"type IN ({_TYPES})"
    )

    op.drop_constraint(
        op.f("ck_scheduled_transfers_destination_xor"), "scheduled_transfers", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_scheduled_transfers_destination_xor"),
        "scheduled_transfers",
        "(payee_id IS NOT NULL AND to_account_id IS NULL) "
        "OR (payee_id IS NULL AND to_account_id IS NOT NULL)",
    )
    op.drop_constraint(
        op.f("uq_scheduled_transfers_loan_id"), "scheduled_transfers", type_="unique"
    )
    op.drop_constraint(
        op.f("fk_scheduled_transfers_loan_id_loans"), "scheduled_transfers", type_="foreignkey"
    )
    op.drop_column("scheduled_transfers", "loan_id")

    op.drop_index("ix_loan_instalments_transaction_id", table_name="loan_instalments")
    op.drop_table("loan_instalments")
    op.drop_index("ix_loans_account_id", table_name="loans")
    op.drop_index("ix_loans_user_id", table_name="loans")
    op.drop_index("ix_loans_customer_id_created_at", table_name="loans")
    op.drop_table("loans")
