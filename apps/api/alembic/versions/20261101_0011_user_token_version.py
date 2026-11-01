"""Access-token revocation: users.token_version (ADR-0021).

Access tokens carry the user's token_version as the `ver` claim; a password change increments it,
and every request checks it (and the user's status), so revoked sessions stop at once instead of
after the 15-minute token lifetime. Existing tokens have no claim and are read as version 0, which
matches the default, so the upgrade logs nobody out.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("token_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_users_token_version_non_negative"), "users", "token_version >= 0"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_users_token_version_non_negative"), "users", type_="check")
    op.drop_column("users", "token_version")
