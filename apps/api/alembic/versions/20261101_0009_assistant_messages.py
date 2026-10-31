"""Assistant conversation store (TASK-060).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-01 00:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TOOLS = (
    "'get_account_summary', 'get_recent_transactions', 'get_spend', 'get_balance_forecast', "
    "'get_loan_affordability'"
)


def upgrade() -> None:
    op.create_table(
        "assistant_messages",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('user', 'assistant')", name=op.f("ck_assistant_messages_role_valid")),
        sa.CheckConstraint(
            "char_length(content) <= 2000", name=op.f("ck_assistant_messages_content_length")
        ),
        sa.CheckConstraint(
            f"tool_name IS NULL OR tool_name IN ({_TOOLS})",
            name=op.f("ck_assistant_messages_tool_name_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_assistant_messages_customer_id_customers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assistant_messages")),
    )
    op.create_index(
        "ix_assistant_messages_customer_id_created_at",
        "assistant_messages",
        ["customer_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_assistant_messages_customer_id_created_at", table_name="assistant_messages")
    op.drop_table("assistant_messages")
