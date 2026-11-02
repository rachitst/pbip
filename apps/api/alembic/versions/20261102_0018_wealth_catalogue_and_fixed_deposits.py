"""Wealth catalogue, fixed deposits, the deposits system account and FD ledger types (TASK-063/064).

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-02 13:39:13.984048+00:00

ADR-0025. ``wealth_products`` is reference data seeded here, identical in every environment: a rate
change is a NEW migration, never an edit of this one. The rates are PBIP's own indicative, simulated
rates (not a real bank's offer); the lock-ins, tenures and coupon of the tax-saver deposit, ELSS fund
and sovereign gold bond follow the published rules of those Indian product types. Market-linked funds
carry no rate because PBIP has no market data.

The downgrade narrows the transaction-type CHECK first: if fixed-deposit transactions exist it fails
and the whole downgrade rolls back, so ledger history is never deleted to make a downgrade pass.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TYPES = (
    "'TRANSFER', 'DEPOSIT', 'REVERSAL', 'FEE', 'BILL_PAYMENT', 'LOAN_DISBURSAL', 'LOAN_REPAYMENT'"
)
SYSTEM_FIXED_DEPOSITS = ("00000000-0000-0000-0000-0000000000c3", "SYS-FIXED-DEPOSITS")
CATALOGUE_VERSION = "2026-11-v1"
_FD_NOTE = (
    "Indicative, simulated rate set by PBIP, not a real bank's offer. Closing early pays the rate "
    "less 1.00% for the time held, and no interest within the first 7 days."
)
_FUND_NOTE = (
    "Market-linked: returns are not guaranteed. PBIP has no market-data feed, so no yield, NAV or "
    "past performance is shown. Listed for comparison; not sold online by PBIP."
)


def _product(
    n: int,
    code: str,
    product_type: str,
    name: str,
    description: str,
    risk: str,
    basis: str,
    rate: int | None,
    tenures: list[int],
    lock_in: int,
    amounts: tuple[int | None, int | None],
    penalty: int | None,
    notes: str,
) -> dict[str, object]:
    return {
        "id": f"20000000-0000-4000-a000-{n:012d}",
        "code": code,
        "product_type": product_type,
        "name": name,
        "description": description,
        "risk_level": risk,
        "yield_basis": basis,
        "annual_rate_bps": rate,
        "tenures_months": tenures,
        "min_lock_in_months": lock_in,
        "min_amount_minor": amounts[0],
        "max_amount_minor": amounts[1],
        "premature_penalty_bps": penalty,
        "bookable": product_type == "FIXED_DEPOSIT",
        "notes": notes,
        "catalogue_version": CATALOGUE_VERSION,
    }


_CUMULATIVE = "Cumulative deposit: interest compounds quarterly and is paid with the principal."
PRODUCTS = [
    _product(1, "FD-SHORT", "FIXED_DEPOSIT", "Fixed deposit, 6 to 9 months", _CUMULATIVE,
             "LOW", "GUARANTEED", 650, [6, 9], 0, (10_000_00, 1_00_00_000_00), 100, _FD_NOTE),
    _product(2, "FD-REGULAR", "FIXED_DEPOSIT", "Fixed deposit, 1 to 3 years", _CUMULATIVE,
             "LOW", "GUARANTEED", 710, [12, 18, 24, 36], 0, (10_000_00, 1_00_00_000_00), 100,
             _FD_NOTE),
    _product(3, "FD-TAX-SAVER", "FIXED_DEPOSIT", "Tax-saver fixed deposit, 5 years",
             _CUMULATIVE + " Can qualify for the section 80C deduction (up to INR 1.5 lakh a year).",
             "LOW", "GUARANTEED", 675, [60], 60, (10_000_00, 1_50_000_00), None,
             "Indicative, simulated rate set by PBIP, not a real bank's offer. Locked in for 5 "
             "years: it cannot be closed before maturity."),
    _product(4, "MF-LIQUID", "MUTUAL_FUND", "Liquid fund (illustrative)",
             "Invests in very short-term money-market instruments; a place to park cash.",
             "LOW", "MARKET_LINKED", None, [], 0, (500_00, None), None, _FUND_NOTE),
    _product(5, "MF-BALANCED", "MUTUAL_FUND", "Balanced advantage fund (illustrative)",
             "Moves between equity and debt according to market valuations.",
             "MODERATE", "MARKET_LINKED", None, [], 0, (500_00, None), None, _FUND_NOTE),
    _product(6, "MF-INDEX", "MUTUAL_FUND", "Nifty 50 index fund (illustrative)",
             "Tracks the Nifty 50 index of large Indian companies.",
             "HIGH", "MARKET_LINKED", None, [], 0, (500_00, None), None, _FUND_NOTE),
    _product(7, "MF-ELSS", "MUTUAL_FUND", "ELSS tax-saver fund (illustrative)",
             "Equity fund with a 3-year lock-in; can qualify for the section 80C deduction.",
             "HIGH", "MARKET_LINKED", None, [], 36, (500_00, None), None, _FUND_NOTE),
    _product(8, "SGB", "SOVEREIGN_GOLD_BOND", "Sovereign gold bond (illustrative)",
             "Government bond in grams of gold: 2.50% a year on the issue value, paid half-yearly, "
             "plus the gold price at redemption.",
             "MODERATE", "FIXED_COUPON", 250, [96], 60, (None, None), None,
             "The rate is the bond's fixed coupon. Returns also depend on the gold price, which "
             "PBIP does not show (no market data). Early exit only from year 5. Not sold online."),
]  # fmt: skip


def _seed() -> None:
    products = sa.table(
        "wealth_products",
        sa.column("id", sa.Uuid()),
        sa.column("code", sa.String),
        sa.column("product_type", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.String),
        sa.column("risk_level", sa.String),
        sa.column("yield_basis", sa.String),
        sa.column("annual_rate_bps", sa.Integer),
        sa.column("tenures_months", postgresql.ARRAY(sa.Integer())),
        sa.column("min_lock_in_months", sa.Integer),
        sa.column("min_amount_minor", sa.BigInteger),
        sa.column("max_amount_minor", sa.BigInteger),
        sa.column("premature_penalty_bps", sa.Integer),
        sa.column("bookable", sa.Boolean),
        sa.column("notes", sa.String),
        sa.column("catalogue_version", sa.String),
    )
    op.bulk_insert(products, PRODUCTS)
    account_id, number = SYSTEM_FIXED_DEPOSITS
    op.execute(
        "INSERT INTO accounts (id, account_number, account_type, currency, status, "
        "ledger_balance, held_amount, overdraft_limit) VALUES "
        f"('{account_id}', '{number}', 'SYSTEM', 'INR', 'ACTIVE', 0, 0, 0)"
    )


def upgrade() -> None:
    op.create_table(
        "wealth_products",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column(
            "product_type",
            sa.Enum(
                "FIXED_DEPOSIT",
                "MUTUAL_FUND",
                "SOVEREIGN_GOLD_BOND",
                name="producttype",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=False),
        sa.Column(
            "risk_level",
            sa.Enum("LOW", "MODERATE", "HIGH", name="risklevel", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column(
            "yield_basis",
            sa.Enum(
                "GUARANTEED",
                "FIXED_COUPON",
                "MARKET_LINKED",
                name="yieldbasis",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("annual_rate_bps", sa.Integer(), nullable=True),
        sa.Column("tenures_months", postgresql.ARRAY(sa.Integer()), nullable=False),
        sa.Column("min_lock_in_months", sa.Integer(), nullable=False),
        sa.Column("min_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("max_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("premature_penalty_bps", sa.Integer(), nullable=True),
        sa.Column("bookable", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.String(length=300), nullable=False),
        sa.Column("catalogue_version", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(yield_basis = 'MARKET_LINKED') = (annual_rate_bps IS NULL)",
            name=op.f("ck_wealth_products_rate_only_when_not_market_linked"),
        ),
        sa.CheckConstraint(
            "NOT bookable OR (product_type = 'FIXED_DEPOSIT' AND annual_rate_bps IS NOT NULL AND min_amount_minor IS NOT NULL AND max_amount_minor IS NOT NULL AND cardinality(tenures_months) > 0)",
            name=op.f("ck_wealth_products_bookable_is_a_complete_deposit"),
        ),
        sa.CheckConstraint(
            "product_type IN ('FIXED_DEPOSIT', 'MUTUAL_FUND', 'SOVEREIGN_GOLD_BOND')",
            name=op.f("ck_wealth_products_product_type_valid"),
        ),
        sa.CheckConstraint(
            "risk_level IN ('LOW', 'MODERATE', 'HIGH')",
            name=op.f("ck_wealth_products_risk_level_valid"),
        ),
        sa.CheckConstraint(
            "yield_basis IN ('GUARANTEED', 'FIXED_COUPON', 'MARKET_LINKED')",
            name=op.f("ck_wealth_products_yield_basis_valid"),
        ),
        sa.CheckConstraint(
            "annual_rate_bps IS NULL OR annual_rate_bps >= 0",
            name=op.f("ck_wealth_products_rate_valid"),
        ),
        sa.CheckConstraint(
            "max_amount_minor IS NULL OR max_amount_minor >= min_amount_minor",
            name=op.f("ck_wealth_products_max_amount_at_least_min"),
        ),
        sa.CheckConstraint(
            "min_amount_minor IS NULL OR min_amount_minor > 0",
            name=op.f("ck_wealth_products_min_amount_positive"),
        ),
        sa.CheckConstraint(
            "min_lock_in_months >= 0", name=op.f("ck_wealth_products_lock_in_non_negative")
        ),
        sa.CheckConstraint(
            "premature_penalty_bps IS NULL OR premature_penalty_bps >= 0",
            name=op.f("ck_wealth_products_penalty_non_negative"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wealth_products")),
        sa.UniqueConstraint("code", name=op.f("uq_wealth_products_code")),
    )
    op.create_index(
        "ix_wealth_products_risk_level", "wealth_products", ["risk_level"], unique=False
    )
    op.create_table(
        "fixed_deposits",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("booking_transaction_id", sa.Uuid(), nullable=False),
        sa.Column("closure_transaction_id", sa.Uuid(), nullable=True),
        sa.Column("interest_transaction_id", sa.Uuid(), nullable=True),
        sa.Column("annual_rate_bps", sa.Integer(), nullable=False),
        sa.Column("tenure_months", sa.Integer(), nullable=False),
        sa.Column("lock_in_months", sa.Integer(), nullable=False),
        sa.Column("premature_penalty_bps", sa.Integer(), nullable=True),
        sa.Column("principal", sa.BigInteger(), nullable=False),
        sa.Column("maturity_interest", sa.BigInteger(), nullable=False),
        sa.Column("maturity_amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("maturity_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "CLOSED", name="fixeddepositstatus", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column(
            "closure_kind",
            sa.Enum("MATURED", "PREMATURE", name="closurekind", native_enum=False, length=32),
            nullable=True,
        ),
        sa.Column("paid_interest", sa.BigInteger(), nullable=True),
        sa.Column("closed_on", sa.Date(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'CLOSED') = (closed_at IS NOT NULL AND closed_on IS NOT NULL AND closure_kind IS NOT NULL AND closure_transaction_id IS NOT NULL AND paid_interest IS NOT NULL)",
            name=op.f("ck_fixed_deposits_closed_has_payout"),
        ),
        sa.CheckConstraint(
            "closure_kind IN ('MATURED', 'PREMATURE')",
            name=op.f("ck_fixed_deposits_closure_kind_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'CLOSED')", name=op.f("ck_fixed_deposits_status_valid")
        ),
        sa.CheckConstraint(
            "annual_rate_bps >= 0", name=op.f("ck_fixed_deposits_rate_non_negative")
        ),
        sa.CheckConstraint(
            "interest_transaction_id IS NULL OR paid_interest > 0",
            name=op.f("ck_fixed_deposits_interest_transaction_only_for_interest"),
        ),
        sa.CheckConstraint(
            "lock_in_months >= 0", name=op.f("ck_fixed_deposits_lock_in_non_negative")
        ),
        sa.CheckConstraint(
            "maturity_amount = principal + maturity_interest",
            name=op.f("ck_fixed_deposits_maturity_amount_is_principal_plus_interest"),
        ),
        sa.CheckConstraint(
            "maturity_date > start_date", name=op.f("ck_fixed_deposits_matures_after_start")
        ),
        sa.CheckConstraint(
            "maturity_interest >= 0", name=op.f("ck_fixed_deposits_maturity_interest_non_negative")
        ),
        sa.CheckConstraint(
            "paid_interest IS NULL OR paid_interest >= 0",
            name=op.f("ck_fixed_deposits_paid_interest_non_negative"),
        ),
        sa.CheckConstraint(
            "premature_penalty_bps IS NULL OR premature_penalty_bps >= 0",
            name=op.f("ck_fixed_deposits_penalty_non_negative"),
        ),
        sa.CheckConstraint("principal > 0", name=op.f("ck_fixed_deposits_principal_positive")),
        sa.CheckConstraint(
            "tenure_months > 0 AND mod(tenure_months, 3) = 0",
            name=op.f("ck_fixed_deposits_tenure_in_quarters"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_fixed_deposits_account_id_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["booking_transaction_id"],
            ["transactions.id"],
            name=op.f("fk_fixed_deposits_booking_transaction_id_transactions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["closure_transaction_id"],
            ["transactions.id"],
            name=op.f("fk_fixed_deposits_closure_transaction_id_transactions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_fixed_deposits_customer_id_customers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["interest_transaction_id"],
            ["transactions.id"],
            name=op.f("fk_fixed_deposits_interest_transaction_id_transactions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["wealth_products.id"],
            name=op.f("fk_fixed_deposits_product_id_wealth_products"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_fixed_deposits_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fixed_deposits")),
        sa.UniqueConstraint(
            "booking_transaction_id", name=op.f("uq_fixed_deposits_booking_transaction_id")
        ),
        sa.UniqueConstraint(
            "closure_transaction_id", name=op.f("uq_fixed_deposits_closure_transaction_id")
        ),
        sa.UniqueConstraint(
            "interest_transaction_id", name=op.f("uq_fixed_deposits_interest_transaction_id")
        ),
    )
    op.create_index("ix_fixed_deposits_account_id", "fixed_deposits", ["account_id"], unique=False)
    op.create_index(
        "ix_fixed_deposits_customer_id_created_at",
        "fixed_deposits",
        ["customer_id", "created_at"],
        unique=False,
    )
    op.create_index("ix_fixed_deposits_product_id", "fixed_deposits", ["product_id"], unique=False)
    op.create_index("ix_fixed_deposits_user_id", "fixed_deposits", ["user_id"], unique=False)
    _seed()
    op.drop_constraint(op.f("ck_transactions_type_valid"), "transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_transactions_type_valid"),
        "transactions",
        f"type IN ({_TYPES}, 'FD_BOOKING', 'FD_CLOSURE', 'FD_INTEREST')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_transactions_type_valid"), "transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_transactions_type_valid"), "transactions", f"type IN ({_TYPES})"
    )
    op.drop_index("ix_fixed_deposits_user_id", table_name="fixed_deposits")
    op.drop_index("ix_fixed_deposits_product_id", table_name="fixed_deposits")
    op.drop_index("ix_fixed_deposits_customer_id_created_at", table_name="fixed_deposits")
    op.drop_index("ix_fixed_deposits_account_id", table_name="fixed_deposits")
    op.drop_table("fixed_deposits")
    op.drop_index("ix_wealth_products_risk_level", table_name="wealth_products")
    op.drop_table("wealth_products")
    # Refused by the entries' foreign key if the account was ever posted to (history is kept).
    op.execute(f"DELETE FROM accounts WHERE id = '{SYSTEM_FIXED_DEPOSITS[0]}'")
