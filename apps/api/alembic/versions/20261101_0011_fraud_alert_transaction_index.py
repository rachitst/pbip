"""Index fraud_alerts.transaction_id: an FK, and now an alert-queue filter (TASK-081).

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_fraud_alerts_transaction_id", "fraud_alerts", ["transaction_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_fraud_alerts_transaction_id", table_name="fraud_alerts")
