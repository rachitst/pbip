"""Bill payments can be held for fraud review (PENDING_REVIEW), like transfers.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_bill_payments_status_valid"), "bill_payments", type_="check")
    op.create_check_constraint(
        op.f("ck_bill_payments_status_valid"),
        "bill_payments",
        "status IN ('POSTED', 'PENDING_REVIEW', 'DECLINED', 'REVERSED')",
    )


def downgrade() -> None:
    # A held payment cannot exist under the old constraint: decline it first. Its funds stay
    # reserved on the account until the hold is released (no ledger write from a migration).
    op.execute(
        "UPDATE bill_payments SET status = 'DECLINED' WHERE status = 'PENDING_REVIEW'"
    )
    op.drop_constraint(op.f("ck_bill_payments_status_valid"), "bill_payments", type_="check")
    op.create_check_constraint(
        op.f("ck_bill_payments_status_valid"),
        "bill_payments",
        "status IN ('POSTED', 'DECLINED', 'REVERSED')",
    )
