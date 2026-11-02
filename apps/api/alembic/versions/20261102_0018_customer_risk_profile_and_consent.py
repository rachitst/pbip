"""Customer risk-tolerance assessment and recommendations consent (TASK-097, TASK-107).

``customers.risk_profile`` (created in 0001, never written) becomes the questionnaire band and gets
the answers, score, questionnaire version and assessment time beside it. The ADR-0015 consent to
use the date of birth for recommendations is a timestamp plus the policy version agreed to.
Nothing is backfilled: every existing customer starts unassessed, with no consent (ADR-0015 rule 5).

Revision ID: 0018
Revises: 0016
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECKS = {
    "risk_profile_valid": "risk_profile IN ('LOW', 'MODERATE', 'HIGH')",
    "risk_score_range": "risk_score BETWEEN 0 AND 10",
    "risk_assessment_complete": (
        "(risk_profile IS NULL AND risk_score IS NULL AND risk_answers IS NULL"
        " AND risk_questionnaire_version IS NULL AND risk_assessed_at IS NULL)"
        " OR (risk_profile IS NOT NULL AND risk_score IS NOT NULL AND risk_answers IS NOT NULL"
        " AND risk_questionnaire_version IS NOT NULL AND risk_assessed_at IS NOT NULL)"
    ),
    "recommendations_consent_complete": (
        "(recommendations_consent_at IS NULL) = (recommendations_consent_policy IS NULL)"
    ),
}


def upgrade() -> None:
    # The column was never written by any code path; clear anything hand-entered so the new
    # completeness check cannot fail on a band without answers (no inference, ADR-0015 rule 5).
    op.execute("UPDATE customers SET risk_profile = NULL WHERE risk_profile IS NOT NULL")
    op.add_column("customers", sa.Column("risk_score", sa.SmallInteger(), nullable=True))
    op.add_column(
        "customers",
        sa.Column("risk_answers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "customers", sa.Column("risk_questionnaire_version", sa.String(length=20), nullable=True)
    )
    op.add_column(
        "customers", sa.Column("risk_assessed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "customers",
        sa.Column("recommendations_consent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "customers",
        sa.Column("recommendations_consent_policy", sa.String(length=40), nullable=True),
    )
    for name, condition in _CHECKS.items():
        op.create_check_constraint(op.f(f"ck_customers_{name}"), "customers", condition)


def downgrade() -> None:
    for name in reversed(list(_CHECKS)):
        op.drop_constraint(op.f(f"ck_customers_{name}"), "customers", type_="check")
    op.drop_column("customers", "recommendations_consent_policy")
    op.drop_column("customers", "recommendations_consent_at")
    op.drop_column("customers", "risk_assessed_at")
    op.drop_column("customers", "risk_questionnaire_version")
    op.drop_column("customers", "risk_answers")
    op.drop_column("customers", "risk_score")
