"""Indexes for hot query paths found by pg_stat_statements under the k6 load suite (TASK-079).

* fraud_events (customer_id, created_at): the customer's recent decisions are read on EVERY scored
  transfer and bill payment; without it the read was a sequential scan that grows with every decision.
* refresh_tokens (user_id): revoke-all-sessions on password change (unindexed foreign key).
* transactions (payee_id): the check before a payee may be deleted (unindexed foreign key).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_fraud_events_customer_id_created_at", "fraud_events", ["customer_id", "created_at"]
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_transactions_payee_id", "transactions", ["payee_id"])


def downgrade() -> None:
    op.drop_index("ix_transactions_payee_id", table_name="transactions")
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_index("ix_fraud_events_customer_id_created_at", table_name="fraud_events")
