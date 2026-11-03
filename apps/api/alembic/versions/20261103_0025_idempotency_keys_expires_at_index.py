"""Index idempotency_keys.expires_at for the daily purge of expired keys (issue #314, ADR-0036).

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-03

The purge job deletes keys with ``expires_at <= now`` in batches; without an index every run scans
the whole table. Downgrade drops the index only: no data changes either way.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_idempotency_keys_expires_at", table_name="idempotency_keys")
