"""Runtime overrides of allow-listed business limits (TASK-108, ADR-0031).

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-03

``runtime_settings`` holds the CURRENT override of a setting an ADMIN may change at runtime; no row
means the environment value applies. The key CHECK is the allow-list (``runtime_config/allowlist.py``):
widening it is a new migration and a reviewed code change. The history of changes (who, old and new
value, reason) is the hash-chained audit log, not this table.

Downgrade drops the table: any overrides are discarded and the environment values apply again (the
audit log still records every change that was made).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KEYS = (
    "'daily_transfer_limit_minor', 'max_transfer_minor', "
    "'max_active_scheduled_transfers_per_customer'"
)


def upgrade() -> None:
    op.create_table(
        "runtime_settings",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.BigInteger(), nullable=False),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"key IN ({_KEYS})", name=op.f("ck_runtime_settings_key_valid")),
        sa.CheckConstraint("value >= 0", name=op.f("ck_runtime_settings_value_non_negative")),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name=op.f("fk_runtime_settings_updated_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runtime_settings")),
        sa.UniqueConstraint("key", name=op.f("uq_runtime_settings_key")),
    )
    op.create_index(
        op.f("ix_runtime_settings_updated_by_user_id"),
        "runtime_settings",
        ["updated_by_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_runtime_settings_updated_by_user_id"), table_name="runtime_settings")
    op.drop_table("runtime_settings")
