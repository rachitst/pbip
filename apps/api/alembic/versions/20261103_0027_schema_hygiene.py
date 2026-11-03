"""Schema hygiene from the LLD (issue #315): enum CHECKs, append-only security_events, FK indexes.

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-03

* CHECKs on the three enum-valued columns that had none: ``security_events.event_type``,
  ``fraud_events.decision`` and ``refresh_tokens.amr``. The allowed values are frozen here (a
  migration must not follow later edits of the Python enums). Before adding them the upgrade
  counts the rows that would violate each one and stops with an error if any exist: existing data
  is never rewritten or deleted to make a constraint fit.
* ``security_events`` becomes append-only in the database, like ``transaction_entries`` and
  ``audit_logs``: the existing ``pbip_forbid_mutation()`` trigger rejects UPDATE and DELETE (no
  code path updates or deletes these rows; test clean-up uses TRUNCATE, which a row trigger does
  not see). A side effect: deleting a user who has security events now fails instead of setting
  ``user_id`` to NULL. Users are disabled, never deleted, so no code path is affected.
* Indexes for the five foreign keys with no index leading with the FK column
  (``transactions.credit_account_id`` was indexed by 0025).
* Renames the CHECKs that migrations 0002 to 0004 named with the ``ck_<table>_`` prefix, which the
  naming convention then added again (``ck_customers_ck_customers_persona_valid``; names over 63
  characters were also truncated with a hash). Cosmetic: the definitions do not change.

Downgrade reverses each step; it changes no data.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SECURITY_EVENT_TYPES = (
    "USER_REGISTERED",
    "LOGIN_SUCCESS",
    "LOGIN_FAILURE",
    "LOGIN_PASSWORD_OK_MFA_PENDING",
    "LOGIN_BLOCKED_LOCKED",
    "ACCOUNT_LOCKED",
    "ACCOUNT_UNLOCKED",
    "MFA_FAILURE",
    "MFA_ENABLED",
    "RECOVERY_CODE_USED",
    "TOKEN_REFRESHED",
    "TOKEN_REUSE_DETECTED",
    "LOGOUT",
    "PASSWORD_CHANGED",
    "STEP_UP_SUCCESS",
    "STEP_UP_FAILURE",
)
FRAUD_DECISIONS = ("ALLOW", "STEP_UP", "REVIEW", "BLOCK")
AMR_VALUES = ("pwd", "pwd,otp")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


# (table, constraint name, condition)
CHECKS = (
    (
        "security_events",
        "ck_security_events_event_type_valid",
        _in("event_type", SECURITY_EVENT_TYPES),
    ),
    ("fraud_events", "ck_fraud_events_decision_valid", _in("decision", FRAUD_DECISIONS)),
    ("refresh_tokens", "ck_refresh_tokens_amr_valid", _in("amr", AMR_VALUES)),
)

# (index name, table, column)
FK_INDEXES = (
    ("ix_transactions_reversal_of_id", "transactions", "reversal_of_id"),
    ("ix_idempotency_keys_transaction_id", "idempotency_keys", "transaction_id"),
    ("ix_fraud_alerts_assigned_to_user_id", "fraud_alerts", "assigned_to_user_id"),
    ("ix_bill_payments_account_id", "bill_payments", "account_id"),
    ("ix_user_roles_role_name", "user_roles", "role_name"),
)

# (schema, table, name in the catalogue, name the naming convention intends)
RENAMES = (
    (
        "synthetic",
        "generation_runs",
        "ck_generation_runs_ck_generation_runs_customers_positive",
        "ck_generation_runs_customers_positive",
    ),  # noqa: E501
    (
        "synthetic",
        "generation_runs",
        "ck_generation_runs_ck_generation_runs_months_range",
        "ck_generation_runs_months_range",
    ),  # noqa: E501
    (
        "synthetic",
        "generation_runs",
        "ck_generation_runs_ck_generation_runs_seed_non_negative",
        "ck_generation_runs_seed_non_negative",
    ),  # noqa: E501
    (
        "synthetic",
        "customers",
        "ck_customers_ck_customers_churn_after_join",
        "ck_customers_churn_after_join",
    ),  # noqa: E501
    (
        "synthetic",
        "customers",
        "ck_customers_ck_customers_emi_consistent",
        "ck_customers_emi_consistent",
    ),  # noqa: E501
    (
        "synthetic",
        "customers",
        "ck_customers_ck_customers_income_positive",
        "ck_customers_income_positive",
    ),  # noqa: E501
    (
        "synthetic",
        "customers",
        "ck_customers_ck_customers_persona_valid",
        "ck_customers_persona_valid",
    ),  # noqa: E501
    (
        "synthetic",
        "customers",
        "ck_customers_ck_customers_savings_rate_range",
        "ck_customers_savings_rate_range",
    ),  # noqa: E501
    (
        "synthetic",
        "transactions",
        "ck_transactions_ck_transactions_amount_positive",
        "ck_transactions_amount_positive",
    ),  # noqa: E501
    (
        "synthetic",
        "transactions",
        "ck_transactions_ck_transactions_direction_valid",
        "ck_transactions_direction_valid",
    ),  # noqa: E501
    (
        "synthetic",
        "transactions",
        "ck_transactions_ck_transactions_event_type_valid",
        "ck_transactions_event_type_valid",
    ),  # noqa: E501
    (
        "public",
        "monthly_category_spending",
        "ck_monthly_category_spending_ck_monthly_category_spendi_b91a",
        "ck_monthly_category_spending_year_valid",
    ),  # noqa: E501
    (
        "public",
        "monthly_category_spending",
        "ck_monthly_category_spending_ck_monthly_category_spendi_80ad",
        "ck_monthly_category_spending_month_valid",
    ),  # noqa: E501
    (
        "public",
        "monthly_category_spending",
        "ck_monthly_category_spending_ck_monthly_category_spendi_d532",
        "ck_monthly_category_spending_total_amount_minor_non_negative",
    ),  # noqa: E501
    (
        "public",
        "monthly_category_spending",
        "ck_monthly_category_spending_ck_monthly_category_spendi_7c93",
        "ck_monthly_category_spending_transaction_count_non_negative",
    ),  # noqa: E501
    (
        "public",
        "customer_segments",
        "ck_customer_segments_ck_customer_segments_segment_valid",
        "ck_customer_segments_segment_valid",
    ),  # noqa: E501
    (
        "public",
        "customer_segments",
        "ck_customer_segments_ck_customer_segments_confidence_sc_d5bb",
        "ck_customer_segments_confidence_score_range",
    ),  # noqa: E501
    (
        "public",
        "customer_segments",
        "ck_customer_segments_ck_customer_segments_recency_days__10fd",
        "ck_customer_segments_recency_days_non_negative",
    ),  # noqa: E501
    (
        "public",
        "customer_segments",
        "ck_customer_segments_ck_customer_segments_frequency_non_b7b2",
        "ck_customer_segments_frequency_non_negative",
    ),  # noqa: E501
    (
        "public",
        "customer_segments",
        "ck_customer_segments_ck_customer_segments_monetary_mino_effd",
        "ck_customer_segments_monetary_minor_non_negative",
    ),  # noqa: E501
    (
        "public",
        "customer_segments",
        "ck_customer_segments_ck_customer_segments_discretionary_dc58",
        "ck_customer_segments_discretionary_ratio_range",
    ),  # noqa: E501
)


def _assert_existing_rows_satisfy_checks() -> None:
    """Fail loudly (and roll the whole upgrade back) if any existing row breaks a new CHECK."""
    bind = op.get_bind()
    violations = []
    for table, name, condition in CHECKS:
        # Table and condition are constants of this module, not input.
        count = bind.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE NOT ({condition})")  # noqa: S608
        ).scalar_one()
        if count:
            violations.append(f"{table}: {count} row(s) violate {name}")
    if violations:
        raise RuntimeError(
            "migration 0027 stopped, existing data would break new CHECK constraints; "
            "fix or remove these rows deliberately, then rerun: " + "; ".join(violations)
        )


def upgrade() -> None:
    _assert_existing_rows_satisfy_checks()
    for table, name, condition in CHECKS:
        op.create_check_constraint(op.f(name), table, condition)
    op.execute(
        "CREATE TRIGGER security_events_immutable BEFORE UPDATE OR DELETE ON security_events "
        "FOR EACH ROW EXECUTE FUNCTION pbip_forbid_mutation()"
    )
    for index, table, column in FK_INDEXES:
        op.create_index(op.f(index), table, [column])
    for schema, table, old, new in RENAMES:
        op.execute(f'ALTER TABLE {schema}.{table} RENAME CONSTRAINT "{old}" TO "{new}"')


def downgrade() -> None:
    for schema, table, old, new in RENAMES:
        op.execute(f'ALTER TABLE {schema}.{table} RENAME CONSTRAINT "{new}" TO "{old}"')
    for index, table, _column in FK_INDEXES:
        op.drop_index(op.f(index), table_name=table)
    op.execute("DROP TRIGGER security_events_immutable ON security_events")
    for table, name, _condition in CHECKS:
        op.drop_constraint(op.f(name), table, type_="check")
