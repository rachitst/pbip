"""Customer behavioral segments schema (TASK-044)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-29 16:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customer_segments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("segment", sa.String(length=32), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("recency_days", sa.Integer(), nullable=False),
        sa.Column("frequency", sa.Float(), nullable=False),
        sa.Column("monetary_minor", sa.BigInteger(), nullable=False),
        sa.Column("savings_ratio", sa.Float(), nullable=False),
        sa.Column("discretionary_ratio", sa.Float(), nullable=False),
        sa.Column("explanation", sa.String(length=512), nullable=False),
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
        sa.CheckConstraint(
            "segment IN ('HIGH_NET_WORTH', 'BUDGET_CONSCIOUS', 'FREQUENT_TRANSACTOR', "
            "'DISCRETIONARY_SPENDER', 'INACTIVE_SAVER')",
            name="ck_customer_segments_segment_valid",
        ),
        sa.CheckConstraint(
            "confidence_score >= 0.0 AND confidence_score <= 1.0",
            name="ck_customer_segments_confidence_score_range",
        ),
        sa.CheckConstraint(
            "recency_days >= 0",
            name="ck_customer_segments_recency_days_non_negative",
        ),
        sa.CheckConstraint(
            "frequency >= 0.0",
            name="ck_customer_segments_frequency_non_negative",
        ),
        sa.CheckConstraint(
            "monetary_minor >= 0",
            name="ck_customer_segments_monetary_minor_non_negative",
        ),
        sa.CheckConstraint(
            "discretionary_ratio >= 0.0 AND discretionary_ratio <= 1.0",
            name="ck_customer_segments_discretionary_ratio_range",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name="fk_customer_segments_customer_id_customers",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_customer_segments"),
        sa.UniqueConstraint("customer_id", name="uq_customer_segments_customer_id"),
    )
    op.create_index(
        "ix_customer_segments_segment",
        "customer_segments",
        ["segment"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_customer_segments_segment", table_name="customer_segments")
    op.drop_table("customer_segments")
