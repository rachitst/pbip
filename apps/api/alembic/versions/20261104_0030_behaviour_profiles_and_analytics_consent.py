"""Analytics consent on customers; behavioural profiles and product interests (UC-11, ADR-0057).

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-04

``customers.analytics_consent`` is added as false for every existing customer: nobody is assumed
to have agreed (no inference or backfill, as ADR-0015 rule 5). The two new tables are written
only by the daily profile job and are empty until it runs. The downgrade drops the tables and the
columns; the consent decisions remain as evidence in the audit log.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here: a migration must not follow later edits of the Python enum (ProductType).
_CATEGORIES = "'LOAN', 'SAVINGS', 'INVESTMENT', 'INSURANCE'"


def _consent_columns() -> None:
    op.add_column(
        "customers",
        sa.Column(
            "analytics_consent", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.add_column(
        "customers",
        sa.Column("analytics_consent_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "customers", sa.Column("analytics_consent_policy", sa.String(length=40), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_customers_analytics_consent_decision_complete"),
        "customers",
        "(analytics_consent_updated_at IS NULL) = (analytics_consent_policy IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_customers_analytics_consent_granted_has_time"),
        "customers",
        "NOT analytics_consent OR analytics_consent_updated_at IS NOT NULL",
    )


def _created_at() -> sa.Column[object]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _profiles() -> None:
    op.create_table(
        "customer_behaviour_profiles",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("window_days", sa.SmallInteger(), nullable=False),
        sa.Column("sessions", sa.Integer(), nullable=False),
        sa.Column("avg_session_seconds", sa.Integer(), nullable=False),
        sa.Column("page_views", sa.Integer(), nullable=False),
        sa.Column("interactions", sa.Integer(), nullable=False),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        _created_at(),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "window_days BETWEEN 1 AND 180",
            name=op.f("ck_customer_behaviour_profiles_window_days_range"),
        ),
        sa.CheckConstraint(
            "sessions >= 0", name=op.f("ck_customer_behaviour_profiles_sessions_non_negative")
        ),
        sa.CheckConstraint(
            "avg_session_seconds >= 0",
            name=op.f("ck_customer_behaviour_profiles_avg_session_seconds_non_negative"),
        ),
        sa.CheckConstraint(
            "interactions >= sessions",
            name=op.f("ck_customer_behaviour_profiles_interactions_cover_sessions"),
        ),
        sa.CheckConstraint(
            "page_views >= 0 AND page_views <= interactions",
            name=op.f("ck_customer_behaviour_profiles_page_views_range"),
        ),
        sa.CheckConstraint(
            "(sessions = 0) = (last_active_at IS NULL)",
            name=op.f("ck_customer_behaviour_profiles_last_active_iff_sessions"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_customer_behaviour_profiles_customer_id_customers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customer_behaviour_profiles")),
        sa.UniqueConstraint("customer_id", name=op.f("uq_customer_behaviour_profiles_customer_id")),
    )


def _interests() -> None:
    op.create_table(
        "customer_product_interests",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("signals", sa.Integer(), nullable=False),
        sa.Column("last_signal_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            f"category IN ({_CATEGORIES})",
            name=op.f("ck_customer_product_interests_category_valid"),
        ),
        sa.CheckConstraint(
            "score >= 0.0", name=op.f("ck_customer_product_interests_score_non_negative")
        ),
        sa.CheckConstraint(
            "signals >= 1", name=op.f("ck_customer_product_interests_signals_positive")
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_customer_product_interests_customer_id_customers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customer_product_interests")),
        sa.UniqueConstraint(
            "customer_id",
            "category",
            name=op.f("uq_customer_product_interests_customer_id_category"),
        ),
    )
    op.create_index(
        op.f("ix_customer_product_interests_category"),
        "customer_product_interests",
        ["category"],
        unique=False,
    )


def upgrade() -> None:
    _consent_columns()
    _profiles()
    _interests()


def downgrade() -> None:
    op.drop_index(
        op.f("ix_customer_product_interests_category"), table_name="customer_product_interests"
    )
    op.drop_table("customer_product_interests")
    op.drop_table("customer_behaviour_profiles")
    op.drop_constraint(
        op.f("ck_customers_analytics_consent_granted_has_time"), "customers", type_="check"
    )
    op.drop_constraint(
        op.f("ck_customers_analytics_consent_decision_complete"), "customers", type_="check"
    )
    op.drop_column("customers", "analytics_consent_policy")
    op.drop_column("customers", "analytics_consent_updated_at")
    op.drop_column("customers", "analytics_consent")
