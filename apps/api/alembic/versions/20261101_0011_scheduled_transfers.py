"""Scheduled and recurring transfers schema (TASK-015 / ADR-0021).

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-01 01:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduled_transfers",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("from_account_id", sa.Uuid(), nullable=False),
        sa.Column("payee_id", sa.Uuid(), nullable=True),
        sa.Column("to_account_id", sa.Uuid(), nullable=True),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("reference", sa.String(length=140), nullable=True),
        sa.Column(
            "frequency",
            sa.Enum(
                "DAILY",
                "WEEKLY",
                "MONTHLY",
                name="schedulefrequency",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "ACTIVE",
                "PAUSED",
                "CANCELLED",
                "COMPLETED",
                name="scheduledtransferstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("next_run_date", sa.Date(), nullable=False),
        sa.Column("last_run_date", sa.Date(), nullable=True),
        sa.Column("last_transaction_id", sa.Uuid(), nullable=True),
        sa.Column("failure_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_failure_reason", sa.String(length=64), nullable=True),
        sa.Column("last_failed_occurrence_date", sa.Date(), nullable=True),
        sa.Column("pause_reason", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_scheduled_transfers_amount_positive")),
        sa.CheckConstraint(
            "failure_count >= 0",
            name=op.f("ck_scheduled_transfers_failure_count_non_negative"),
        ),
        sa.CheckConstraint(
            "frequency IN ('DAILY', 'WEEKLY', 'MONTHLY')",
            name=op.f("ck_scheduled_transfers_frequency_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'PAUSED', 'CANCELLED', 'COMPLETED')",
            name=op.f("ck_scheduled_transfers_status_valid"),
        ),
        sa.CheckConstraint(
            "(payee_id IS NOT NULL AND to_account_id IS NULL) "
            "OR (payee_id IS NULL AND to_account_id IS NOT NULL)",
            name=op.f("ck_scheduled_transfers_destination_xor"),
        ),
        sa.CheckConstraint(
            "end_date IS NULL OR end_date >= start_date",
            name=op.f("ck_scheduled_transfers_end_date_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'PAUSED' AND pause_reason IS NOT NULL AND pause_reason IN "
            "('CUSTOMER', 'FRAUD_BLOCK', 'CUSTOMER_ACTION_REQUIRED')) "
            "OR (status <> 'PAUSED' AND pause_reason IS NULL)",
            name=op.f("ck_scheduled_transfers_pause_reason_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_scheduled_transfers_customer_id_customers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_scheduled_transfers_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["from_account_id"],
            ["accounts.id"],
            name=op.f("fk_scheduled_transfers_from_account_id_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payee_id"],
            ["payees.id"],
            name=op.f("fk_scheduled_transfers_payee_id_payees"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["to_account_id"],
            ["accounts.id"],
            name=op.f("fk_scheduled_transfers_to_account_id_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["last_transaction_id"],
            ["transactions.id"],
            name=op.f("fk_scheduled_transfers_last_transaction_id_transactions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduled_transfers")),
    )
    op.create_index(
        "ix_scheduled_transfers_status_next_run_date",
        "scheduled_transfers",
        ["status", "next_run_date"],
        unique=False,
    )
    op.create_index(
        "ix_scheduled_transfers_customer_id_created_at",
        "scheduled_transfers",
        ["customer_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_scheduled_transfers_user_id",
        "scheduled_transfers",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_scheduled_transfers_from_account_id",
        "scheduled_transfers",
        ["from_account_id"],
        unique=False,
    )
    op.create_index(
        "ix_scheduled_transfers_payee_id",
        "scheduled_transfers",
        ["payee_id"],
        unique=False,
    )
    op.create_index(
        "ix_scheduled_transfers_to_account_id",
        "scheduled_transfers",
        ["to_account_id"],
        unique=False,
    )
    op.create_index(
        "ix_scheduled_transfers_last_transaction_id",
        "scheduled_transfers",
        ["last_transaction_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_transfers_last_transaction_id", table_name="scheduled_transfers")
    op.drop_index("ix_scheduled_transfers_to_account_id", table_name="scheduled_transfers")
    op.drop_index("ix_scheduled_transfers_payee_id", table_name="scheduled_transfers")
    op.drop_index("ix_scheduled_transfers_from_account_id", table_name="scheduled_transfers")
    op.drop_index("ix_scheduled_transfers_user_id", table_name="scheduled_transfers")
    op.drop_index("ix_scheduled_transfers_customer_id_created_at", table_name="scheduled_transfers")
    op.drop_index("ix_scheduled_transfers_status_next_run_date", table_name="scheduled_transfers")
    op.drop_table("scheduled_transfers")
