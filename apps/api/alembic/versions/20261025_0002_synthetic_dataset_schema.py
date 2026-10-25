"""synthetic dataset schema (data-gen v1, ADR 0009)

Offline ML training data lives in its own ``synthetic`` schema. It is never joined to, or
written into, the operational ledger (``public.transaction_entries`` stays writable only by
``ledger/service.py``). The schema has no ORM models: it is written by ``pbip_datagen.loader``
with COPY and read by notebooks/feature pipelines, and ``alembic check`` compares the default
schema only.

Integrity is enforced in the database but SET-BASED, because per-row FK triggers dominated COPY
time (measured: ~85% of a 400k-row load). ``transactions`` therefore has no row-level FKs;
instead statement-level triggers with transition tables check every inserted row against its
customer and run in one join, cascade customer deletes, and make both tables append-only.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25 08:00:00.000000+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "synthetic"
PERSONAS = ("salaried_professional", "student", "freelancer", "business_owner", "retiree")
EVENT_TYPES = (
    "SALARY", "PENSION", "ALLOWANCE", "FREELANCE_INCOME", "BUSINESS_INCOME",
    "EMI", "UTILITY", "DISCRETIONARY",
)


TRIGGERS = (
    # Every inserted transaction must belong to an existing customer OF THE SAME RUN.
    f"""
    CREATE FUNCTION {SCHEMA}.check_transaction_customers() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM new_rows n
            LEFT JOIN {SCHEMA}.customers c ON c.id = n.customer_id AND c.run_id = n.run_id
            WHERE c.id IS NULL
        ) THEN
            RAISE EXCEPTION 'synthetic transaction references a customer outside its run'
                USING ERRCODE = 'foreign_key_violation';
        END IF;
        RETURN NULL;
    END $$
    """,
    f"""
    CREATE TRIGGER trg_transactions_customer_exists
    AFTER INSERT ON {SCHEMA}.transactions REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.check_transaction_customers()
    """,
    # Deleting customers (directly, or via the run's ON DELETE CASCADE) removes their rows.
    f"""
    CREATE FUNCTION {SCHEMA}.cascade_customer_delete() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        DELETE FROM {SCHEMA}.transactions t USING old_rows o WHERE t.customer_id = o.id;
        RETURN NULL;
    END $$
    """,
    f"""
    CREATE TRIGGER trg_customers_cascade_delete
    AFTER DELETE ON {SCHEMA}.customers REFERENCING OLD TABLE AS old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.cascade_customer_delete()
    """,
    # A generated dataset is a fixed artefact of (version, seed, config): rows are never edited,
    # a run is replaced wholesale. This also rules out re-pointing rows to orphan them.
    f"""
    CREATE FUNCTION {SCHEMA}.reject_mutation() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'synthetic dataset rows are immutable; reload the run instead'
            USING ERRCODE = 'restrict_violation';
    END $$
    """,
    f"""
    CREATE TRIGGER trg_customers_immutable BEFORE UPDATE ON {SCHEMA}.customers
    FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_mutation()
    """,
    f"""
    CREATE TRIGGER trg_transactions_immutable BEFORE UPDATE ON {SCHEMA}.transactions
    FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_mutation()
    """,
    # TRUNCATE skips row/statement DELETE triggers, so refuse to truncate customers while
    # transactions remain (truncate transactions first, or delete runs).
    f"""
    CREATE FUNCTION {SCHEMA}.guard_customer_truncate() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF EXISTS (SELECT 1 FROM {SCHEMA}.transactions) THEN
            RAISE EXCEPTION 'truncate synthetic.transactions before synthetic.customers'
                USING ERRCODE = 'foreign_key_violation';
        END IF;
        RETURN NULL;
    END $$
    """,
    f"""
    CREATE TRIGGER trg_customers_guard_truncate BEFORE TRUNCATE ON {SCHEMA}.customers
    FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.guard_customer_truncate()
    """,
)


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    op.create_table('generation_runs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('generator_version', sa.String(length=20), nullable=False),
    sa.Column('seed', sa.BigInteger(), nullable=False),
    sa.Column('customers', sa.Integer(), nullable=False),
    sa.Column('months', sa.Integer(), nullable=False),
    sa.Column('start_date', sa.Date(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('seed >= 0', name='ck_generation_runs_seed_non_negative'),
    sa.CheckConstraint('customers > 0', name='ck_generation_runs_customers_positive'),
    sa.CheckConstraint('months BETWEEN 1 AND 120', name='ck_generation_runs_months_range'),
    sa.PrimaryKeyConstraint('id', name='pk_generation_runs'),
    schema=SCHEMA,
    )
    op.create_table('customers',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('customer_index', sa.Integer(), nullable=False),
    sa.Column('persona', sa.String(length=32), nullable=False),
    sa.Column('city', sa.String(length=40), nullable=False),
    sa.Column('monthly_income_minor', sa.BigInteger(), nullable=False),
    sa.Column('savings_rate_bps', sa.Integer(), nullable=False),
    sa.Column('has_emi', sa.Boolean(), nullable=False),
    sa.Column('emi_minor', sa.BigInteger(), nullable=False),
    sa.Column('emi_day', sa.SmallInteger(), nullable=True),
    sa.Column('joined_on', sa.Date(), nullable=False),
    sa.Column('churned_on', sa.Date(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint(_in('persona', PERSONAS), name='ck_customers_persona_valid'),
    sa.CheckConstraint('monthly_income_minor > 0', name='ck_customers_income_positive'),
    sa.CheckConstraint('savings_rate_bps BETWEEN 0 AND 10000', name='ck_customers_savings_rate_range'),
    sa.CheckConstraint(
        '(has_emi AND emi_minor > 0 AND emi_day BETWEEN 1 AND 31) '
        'OR (NOT has_emi AND emi_minor = 0 AND emi_day IS NULL)',
        name='ck_customers_emi_consistent',
    ),
    sa.CheckConstraint('churned_on IS NULL OR churned_on >= joined_on', name='ck_customers_churn_after_join'),
    sa.ForeignKeyConstraint(['run_id'], [f'{SCHEMA}.generation_runs.id'], name='fk_customers_run_id_generation_runs', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_customers'),
    sa.UniqueConstraint('run_id', 'customer_index', name='uq_customers_run_id_customer_index'),
    schema=SCHEMA,
    )
    op.create_table('transactions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('customer_id', sa.Uuid(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('direction', sa.String(length=6), nullable=False),
    sa.Column('amount_minor', sa.BigInteger(), nullable=False),
    sa.Column('category', sa.String(length=32), nullable=False),
    sa.Column('event_type', sa.String(length=32), nullable=False),
    sa.Column('merchant', sa.String(length=80), nullable=False),
    sa.CheckConstraint("direction IN ('CREDIT', 'DEBIT')", name='ck_transactions_direction_valid'),
    sa.CheckConstraint('amount_minor > 0', name='ck_transactions_amount_positive'),
    sa.CheckConstraint(_in('event_type', EVENT_TYPES), name='ck_transactions_event_type_valid'),
    sa.PrimaryKeyConstraint('id', name='pk_transactions'),
    schema=SCHEMA,
    )
    op.create_index('ix_customers_run_id', 'customers', ['run_id'], unique=False, schema=SCHEMA)
    op.create_index('ix_transactions_customer_id_occurred_at', 'transactions', ['customer_id', 'occurred_at'], unique=False, schema=SCHEMA)
    op.create_index('ix_transactions_run_id', 'transactions', ['run_id'], unique=False, schema=SCHEMA)
    for statement in TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    op.drop_table('transactions', schema=SCHEMA)
    op.drop_table('customers', schema=SCHEMA)
    op.drop_table('generation_runs', schema=SCHEMA)
    for function in ("check_transaction_customers", "cascade_customer_delete",
                     "reject_mutation", "guard_customer_truncate"):
        op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.{function}()")
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}")
