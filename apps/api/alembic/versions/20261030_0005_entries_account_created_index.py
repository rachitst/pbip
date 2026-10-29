"""Index transaction entries by account and time (audit M1, TASK-040/041)

The spending breakdown, trends and health score select one account's entries in a time window;
the only per-account index was (account_id, seq), so those queries walked the account's whole
history. Additive index, no data change.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-30 10:00:00.000000+00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_transaction_entries_account_id_created_at",
        "transaction_entries",
        ["account_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_transaction_entries_account_id_created_at", table_name="transaction_entries")
