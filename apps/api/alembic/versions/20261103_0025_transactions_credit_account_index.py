"""Index transactions(credit_account_id, created_at) for MULE_PASS_THROUGH (#144, ADR-0035).

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-03

The rule sums the inbound transfers credited to the debited account in the last few minutes on
every scored transfer. Without this index that is a scan of all of the account's credits (and the
FK had no index of its own). Downgrade drops the index; nothing else depends on it.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_transactions_credit_account_id_created_at",
        "transactions",
        ["credit_account_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_transactions_credit_account_id_created_at", table_name="transactions")
