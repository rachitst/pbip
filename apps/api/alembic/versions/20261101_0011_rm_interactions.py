"""Relationship Manager outreach log (TASK-066, ADR-0021).

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-01
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
        "rm_interactions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("staff_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "channel",
            sa.Enum(
                "CALL",
                "MEETING",
                "EMAIL",
                "NOTE",
                name="interactionchannel",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "outcome",
            sa.Enum(
                "INTERESTED",
                "NOT_INTERESTED",
                "FOLLOW_UP",
                "NO_ANSWER",
                "INFORMATION_SENT",
                "ACCOUNT_REVIEW",
                name="interactionoutcome",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "channel IN ('CALL', 'MEETING', 'EMAIL', 'NOTE')",
            name=op.f("ck_rm_interactions_channel_valid"),
        ),
        sa.CheckConstraint(
            "outcome IN ('INTERESTED', 'NOT_INTERESTED', 'FOLLOW_UP', 'NO_ANSWER', "
            "'INFORMATION_SENT', 'ACCOUNT_REVIEW')",
            name=op.f("ck_rm_interactions_outcome_valid"),
        ),
        sa.CheckConstraint(
            "char_length(note) BETWEEN 1 AND 1000", name=op.f("ck_rm_interactions_note_length")
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_rm_interactions_customer_id_customers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["staff_user_id"],
            ["users.id"],
            name=op.f("fk_rm_interactions_staff_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rm_interactions")),
    )
    op.create_index(
        "ix_rm_interactions_customer_id_created_at",
        "rm_interactions",
        ["customer_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_rm_interactions_staff_user_id", "rm_interactions", ["staff_user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_rm_interactions_staff_user_id", table_name="rm_interactions")
    op.drop_index("ix_rm_interactions_customer_id_created_at", table_name="rm_interactions")
    op.drop_table("rm_interactions")
