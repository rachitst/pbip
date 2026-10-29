"""Analytics monthly spending schema (TASK-040)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-29 14:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "monthly_category_spending",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("year", sa.SmallInteger(), nullable=False),
        sa.Column("month", sa.SmallInteger(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("total_amount_minor", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("transaction_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("year >= 2000", name="ck_monthly_category_spending_year_valid"),
        sa.CheckConstraint(
            "month BETWEEN 1 AND 12", name="ck_monthly_category_spending_month_valid"
        ),
        sa.CheckConstraint(
            "total_amount_minor >= 0",
            name="ck_monthly_category_spending_total_amount_minor_non_negative",
        ),
        sa.CheckConstraint(
            "transaction_count >= 0",
            name="ck_monthly_category_spending_transaction_count_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_monthly_category_spending_account_id_accounts",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_monthly_category_spending"),
        sa.UniqueConstraint(
            "account_id",
            "year",
            "month",
            "category",
            name="uq_monthly_category_spending_account_period_cat",
        ),
    )
    op.create_index(
        "ix_monthly_category_spending_account_period",
        "monthly_category_spending",
        ["account_id", "year", "month"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_monthly_category_spending_account_period",
        table_name="monthly_category_spending",
    )
    op.drop_table("monthly_category_spending")
