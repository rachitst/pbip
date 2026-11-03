"""Remove the never-used FEE transaction type from the ledger's type CHECK (issue #175).

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-04

No code path has ever created a ``FEE`` transaction: there is no fees engine (blueprint section 3,
"no interest accrual/fees engine"). The member only made the assistant's ``fees`` spending
category reachable in theory and always empty in practice. A future fees engine adds the type
back with its own migration.

Before narrowing the CHECK the upgrade counts ``FEE`` rows and stops with an error if any exist:
ledger rows are immutable and are never rewritten or deleted to make a constraint fit. The
downgrade restores the previous CHECK exactly; it changes no data.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here: a migration must not follow later edits of the Python enum.
_KEPT = (
    "'TRANSFER', 'DEPOSIT', 'REVERSAL', 'BILL_PAYMENT', 'LOAN_DISBURSAL', 'LOAN_REPAYMENT', "
    "'FD_BOOKING', 'FD_CLOSURE', 'FD_INTEREST'"
)
_BEFORE = (
    "'TRANSFER', 'DEPOSIT', 'REVERSAL', 'FEE', 'BILL_PAYMENT', 'LOAN_DISBURSAL', "
    "'LOAN_REPAYMENT', 'FD_BOOKING', 'FD_CLOSURE', 'FD_INTEREST'"
)


def _replace_type_check(allowed: str) -> None:
    op.drop_constraint(op.f("ck_transactions_type_valid"), "transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_transactions_type_valid"), "transactions", f"type IN ({allowed})"
    )


def upgrade() -> None:
    fee_rows = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM transactions WHERE type = 'FEE'"))
        .scalar_one()
    )
    if fee_rows:
        raise RuntimeError(
            f"migration 0029 stopped: {fee_rows} transaction(s) have type FEE. Ledger rows are "
            "immutable; keep the FEE type (do not apply this migration) or decide what those "
            "rows mean before removing it."
        )
    _replace_type_check(_KEPT)


def downgrade() -> None:
    _replace_type_check(_BEFORE)
