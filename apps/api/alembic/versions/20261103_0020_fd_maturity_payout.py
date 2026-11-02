"""Fixed-deposit maturity payout: the held-payout marker and the poll index (issue #332).

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-03

ADR-0029. ``payout_held_at`` records that the automatic payout found the source account not active
(the customer is notified once per hold); it can only be set while the deposit is open. The index
serves the hourly poll ``status = 'ACTIVE' AND maturity_date <= today``. Downgrade drops both; the
marker is operational state only (no money or ledger history is lost).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fixed_deposits", sa.Column("payout_held_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_fixed_deposits_payout_held_only_while_open"),
        "fixed_deposits",
        "payout_held_at IS NULL OR status = 'ACTIVE'",
    )
    op.create_index(
        op.f("ix_fixed_deposits_status_maturity_date"),
        "fixed_deposits",
        ["status", "maturity_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_fixed_deposits_status_maturity_date"), table_name="fixed_deposits")
    op.drop_constraint(
        op.f("ck_fixed_deposits_payout_held_only_while_open"), "fixed_deposits", type_="check"
    )
    op.drop_column("fixed_deposits", "payout_held_at")
