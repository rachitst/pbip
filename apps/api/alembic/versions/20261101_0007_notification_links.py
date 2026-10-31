"""Optional link target on notifications (TASK-022 / #45).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-01 00:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("notifications", sa.Column("resource_type", sa.String(length=20), nullable=True))
    op.add_column("notifications", sa.Column("resource_id", sa.Uuid(), nullable=True))
    op.add_column("notifications", sa.Column("account_id", sa.Uuid(), nullable=True))
    op.create_check_constraint(
        op.f("ck_notifications_resource_type_valid"),
        "notifications",
        "resource_type IS NULL OR resource_type IN ('transaction', 'security')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_notifications_resource_type_valid"), "notifications", type_="check")
    op.drop_column("notifications", "account_id")
    op.drop_column("notifications", "resource_id")
    op.drop_column("notifications", "resource_type")
