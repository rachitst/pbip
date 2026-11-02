"""Relationship-manager-to-customer assignment with history (TASK-056 follow-up, ADR-0032).

One row per assignment period; reassigning closes the active row instead of deleting it. The partial
unique index allows one active RM per customer. Downgrade drops the table and with it the assignment
history (every change is also in the audit chain; no ledger data lives here).

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rm_assignments",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("rm_user_id", sa.Uuid(), nullable=False),
        sa.Column("assigned_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("unassigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unassigned_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(unassigned_at IS NULL) = (unassigned_by_user_id IS NULL)",
            name=op.f("ck_rm_assignments_unassigned_by_set"),
        ),
        sa.CheckConstraint(
            "unassigned_at IS NULL OR unassigned_at >= assigned_at",
            name=op.f("ck_rm_assignments_unassigned_after_assigned"),
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by_user_id"],
            ["users.id"],
            name=op.f("fk_rm_assignments_assigned_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_rm_assignments_customer_id_customers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rm_user_id"],
            ["users.id"],
            name=op.f("fk_rm_assignments_rm_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["unassigned_by_user_id"],
            ["users.id"],
            name=op.f("fk_rm_assignments_unassigned_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rm_assignments")),
    )
    op.create_index(
        "ix_rm_assignments_assigned_by_user_id", "rm_assignments", ["assigned_by_user_id"]
    )
    op.create_index(
        "ix_rm_assignments_customer_id_assigned_at",
        "rm_assignments",
        ["customer_id", "assigned_at"],
    )
    op.create_index("ix_rm_assignments_rm_user_id", "rm_assignments", ["rm_user_id"])
    op.create_index(
        "ix_rm_assignments_unassigned_by_user_id", "rm_assignments", ["unassigned_by_user_id"]
    )
    op.create_index(
        "uq_rm_assignments_active_customer_id",
        "rm_assignments",
        ["customer_id"],
        unique=True,
        postgresql_where=sa.text("unassigned_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_rm_assignments_active_customer_id", table_name="rm_assignments")
    op.drop_index("ix_rm_assignments_unassigned_by_user_id", table_name="rm_assignments")
    op.drop_index("ix_rm_assignments_rm_user_id", table_name="rm_assignments")
    op.drop_index("ix_rm_assignments_customer_id_assigned_at", table_name="rm_assignments")
    op.drop_index("ix_rm_assignments_assigned_by_user_id", table_name="rm_assignments")
    op.drop_table("rm_assignments")
