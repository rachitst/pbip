"""Investing (funds, stocks, bonds), fund SIPs and insurance applications (ADR-0058).

Revision ID: 0031
Revises: 0029
Create Date: 2026-11-04 18:05:04.996997+00:00

``investment_instruments`` is reference data seeded here, identical in every environment: a
parameter change is a NEW migration, never an edit of this one. The prices those parameters
produce (``wealth/pricing.py``) are SIMULATED, not market data; the three stock companies are
fictional. ELSS and the gold bond are not investable (their lock-ins need lot-level holdings).

Adds the system account ``SYS-INVESTMENTS`` and the ledger types ``INVESTMENT_PURCHASE`` and
``INVESTMENT_SALE``. The downgrade narrows the type CHECK first: if investment transactions exist
it fails and the whole downgrade rolls back, so ledger history is never deleted to make a downgrade
pass (as in migrations 0018 and 0029).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here: a migration must not follow later edits of the Python enum.
_TYPES_BEFORE = (
    "'TRANSFER', 'DEPOSIT', 'REVERSAL', 'BILL_PAYMENT', 'LOAN_DISBURSAL', 'LOAN_REPAYMENT', "
    "'FD_BOOKING', 'FD_CLOSURE', 'FD_INTEREST'"
)
_TYPES_AFTER = f"{_TYPES_BEFORE}, 'INVESTMENT_PURCHASE', 'INVESTMENT_SALE'"
SYSTEM_INVESTMENTS = ("00000000-0000-0000-0000-0000000000c4", "SYS-INVESTMENTS")
CATALOGUE_VERSION = "2026-11-v2"
_FUND_NOTE = (
    "Simulated NAV from PBIP's price model, not market data; returns are not guaranteed. "
    "Units are bought by amount and rounded down to six decimals."
)
_STOCK_NOTE = (
    "Fictional company with a simulated share price from PBIP's price model, not market data. "
    "Bought and sold in whole shares."
)
_BOND_NOTE = (
    "Simulated accumulating bond: the coupon is reflected in the simulated price and is not paid "
    "out. Not market data. Bought and sold in whole bonds of INR 1,000 face value."
)


def _instrument(
    n: int,
    code: str,
    kind: str,
    name: str,
    description: str,
    risk: str,
    product: int | None,
    price: tuple[int, int, int],
    notes: str,
) -> dict[str, object]:
    return {
        "id": f"50000000-0000-4000-a000-{n:012d}",  # 30000000-... is the insurance catalogue
        "code": code,
        "kind": kind,
        "name": name,
        "description": description,
        "risk_level": risk,
        "wealth_product_id": None if product is None else f"20000000-0000-4000-a000-{product:012d}",
        "currency": "INR",
        "min_order_minor": 500_00 if kind == "MUTUAL_FUND" else None,
        "price_base_minor": price[0],
        "price_drift_bps": price[1],
        "price_wobble_bps": price[2],
        "notes": notes,
        "catalogue_version": CATALOGUE_VERSION,
    }


# (base price in paise on 2026-01-01, trend bps a year, largest daily deviation bps)
INSTRUMENTS = [
    _instrument(1, "MF-LIQUID", "MUTUAL_FUND", "Liquid fund (illustrative)",
                "Invests in very short-term money-market instruments; a place to park cash.",
                "LOW", 4, (1_000_00, 650, 5), _FUND_NOTE),
    _instrument(2, "MF-BALANCED", "MUTUAL_FUND", "Balanced advantage fund (illustrative)",
                "Moves between equity and debt according to market valuations.",
                "MODERATE", 5, (45_00, 1000, 80), _FUND_NOTE),
    _instrument(3, "MF-INDEX", "MUTUAL_FUND", "Nifty 50 index fund (illustrative)",
                "Tracks the Nifty 50 index of large Indian companies.",
                "HIGH", 6, (220_00, 1200, 150), _FUND_NOTE),
    _instrument(4, "STK-SIMINFRA", "STOCK", "Simulated Infrastructure Ltd (fictional)",
                "A fictional builder of roads and power lines, for practising stock orders.",
                "HIGH", None, (845_50, 1100, 250), _STOCK_NOTE),
    _instrument(5, "STK-SIMBANK", "STOCK", "Simulated Lending Bank Ltd (fictional)",
                "A fictional private-sector lender, for practising stock orders.",
                "HIGH", None, (1_520_00, 900, 200), _STOCK_NOTE),
    _instrument(6, "STK-SIMSOFT", "STOCK", "Simulated Software Services Ltd (fictional)",
                "A fictional IT services company, for practising stock orders.",
                "HIGH", None, (3_410_00, 1300, 300), _STOCK_NOTE),
    _instrument(7, "BND-GSEC-7Y", "BOND", "Simulated 7-year government bond",
                "A government security with a 7.10% simulated yield, accumulating.",
                "LOW", None, (1_000_00, 710, 20), _BOND_NOTE),
    _instrument(8, "BND-AAA-5Y", "BOND", "Simulated AAA corporate bond, 5 years",
                "A top-rated corporate bond with a 7.80% simulated yield, accumulating.",
                "MODERATE", None, (1_000_00, 780, 35), _BOND_NOTE),
]  # fmt: skip


def _seed() -> None:
    instruments = sa.table(
        "investment_instruments",
        sa.column("id", sa.Uuid()),
        sa.column("code", sa.String),
        sa.column("kind", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.String),
        sa.column("risk_level", sa.String),
        sa.column("wealth_product_id", sa.Uuid()),
        sa.column("currency", sa.String),
        sa.column("min_order_minor", sa.BigInteger),
        sa.column("price_base_minor", sa.BigInteger),
        sa.column("price_drift_bps", sa.Integer),
        sa.column("price_wobble_bps", sa.Integer),
        sa.column("notes", sa.String),
        sa.column("catalogue_version", sa.String),
    )
    op.bulk_insert(instruments, INSTRUMENTS)
    account_id, number = SYSTEM_INVESTMENTS
    op.execute(
        "INSERT INTO accounts (id, account_number, account_type, currency, status, "
        "ledger_balance, held_amount, overdraft_limit) VALUES "
        f"('{account_id}', '{number}', 'SYSTEM', 'INR', 'ACTIVE', 0, 0, 0)"
    )


# The three investable funds' catalogue notes said "not sold online by PBIP" (migration 0018); they
# can now be bought, so their rows are versioned 2026-11-v2 with the new note. The downgrade puts
# the 0018 note and version back exactly.
INVESTABLE_FUND_CODES = ("MF-LIQUID", "MF-BALANCED", "MF-INDEX")
FUND_NOTE_BEFORE = (
    "Market-linked: returns are not guaranteed. PBIP has no market-data feed, so no yield, NAV or "
    "past performance is shown. Listed for comparison; not sold online by PBIP."
)
FUND_NOTE_AFTER = (
    "Market-linked: returns are not guaranteed. Can be bought online at a simulated NAV from "
    "PBIP's price model, not market data; no yield or past performance is shown."
)


def _set_fund_notes(note: str, version: str) -> None:
    products = sa.table(
        "wealth_products",
        sa.column("code", sa.String),
        sa.column("notes", sa.String),
        sa.column("catalogue_version", sa.String),
    )
    op.execute(
        products.update()
        .where(products.c.code.in_(INVESTABLE_FUND_CODES))
        .values(notes=note, catalogue_version=version)
    )


def _replace_type_check(allowed: str) -> None:
    op.drop_constraint(op.f("ck_transactions_type_valid"), "transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_transactions_type_valid"), "transactions", f"type IN ({allowed})"
    )


def upgrade() -> None:
    op.create_table(
        "investment_instruments",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "MUTUAL_FUND", "STOCK", "BOND", name="instrumentkind", native_enum=False, length=32
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
        sa.Column("wealth_product_id", sa.Uuid(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("min_order_minor", sa.BigInteger(), nullable=True),
        sa.Column("price_base_minor", sa.BigInteger(), nullable=False),
        sa.Column("price_drift_bps", sa.Integer(), nullable=False),
        sa.Column("price_wobble_bps", sa.Integer(), nullable=False),
        sa.Column("notes", sa.String(length=300), nullable=False),
        sa.Column("catalogue_version", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(kind = 'MUTUAL_FUND') = (min_order_minor IS NOT NULL)",
            name=op.f("ck_investment_instruments_min_order_only_for_funds"),
        ),
        sa.CheckConstraint(
            "(kind = 'MUTUAL_FUND') = (wealth_product_id IS NOT NULL)",
            name=op.f("ck_investment_instruments_fund_links_its_catalogue_row"),
        ),
        sa.CheckConstraint(
            "kind IN ('MUTUAL_FUND', 'STOCK', 'BOND')",
            name=op.f("ck_investment_instruments_kind_valid"),
        ),
        sa.CheckConstraint(
            "risk_level IN ('LOW', 'MODERATE', 'HIGH')",
            name=op.f("ck_investment_instruments_risk_level_valid"),
        ),
        sa.CheckConstraint(
            "min_order_minor IS NULL OR min_order_minor > 0",
            name=op.f("ck_investment_instruments_min_order_positive"),
        ),
        sa.CheckConstraint(
            "price_base_minor > 0", name=op.f("ck_investment_instruments_price_base_positive")
        ),
        sa.CheckConstraint(
            "price_drift_bps BETWEEN -5000 AND 5000",
            name=op.f("ck_investment_instruments_price_drift_bounded"),
        ),
        sa.CheckConstraint(
            "price_wobble_bps BETWEEN 0 AND 2000",
            name=op.f("ck_investment_instruments_price_wobble_bounded"),
        ),
        sa.ForeignKeyConstraint(
            ["wealth_product_id"],
            ["wealth_products.id"],
            name=op.f("fk_investment_instruments_wealth_product_id_wealth_products"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investment_instruments")),
        sa.UniqueConstraint("code", name=op.f("uq_investment_instruments_code")),
    )
    op.create_index(
        "ix_investment_instruments_wealth_product_id",
        "investment_instruments",
        ["wealth_product_id"],
        unique=False,
    )
    op.create_table(
        "fund_sips",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("day_of_month", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "CANCELLED", name="sipstatus", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("next_run_on", sa.Date(), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'CANCELLED') = (cancelled_at IS NOT NULL)",
            name=op.f("ck_fund_sips_cancelled_has_time"),
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'CANCELLED')", name=op.f("ck_fund_sips_status_valid")
        ),
        sa.CheckConstraint("amount_minor > 0", name=op.f("ck_fund_sips_amount_positive")),
        sa.CheckConstraint(
            "day_of_month BETWEEN 1 AND 28", name=op.f("ck_fund_sips_day_of_month_valid")
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_fund_sips_account_id_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_fund_sips_customer_id_customers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["investment_instruments.id"],
            name=op.f("fk_fund_sips_instrument_id_investment_instruments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_fund_sips_user_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fund_sips")),
    )
    op.create_index("ix_fund_sips_account_id", "fund_sips", ["account_id"], unique=False)
    op.create_index(
        "ix_fund_sips_customer_id_created_at",
        "fund_sips",
        ["customer_id", "created_at"],
        unique=False,
    )
    op.create_index("ix_fund_sips_instrument_id", "fund_sips", ["instrument_id"], unique=False)
    op.create_index(
        "ix_fund_sips_status_next_run_on", "fund_sips", ["status", "next_run_on"], unique=False
    )
    op.create_index("ix_fund_sips_user_id", "fund_sips", ["user_id"], unique=False)
    op.create_table(
        "insurance_applications",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("cover_minor", sa.BigInteger(), nullable=False),
        sa.Column("term_years", sa.Integer(), nullable=False),
        sa.Column("age_band", sa.String(length=8), nullable=False),
        sa.Column("annual_premium_minor", sa.BigInteger(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "SUBMITTED",
                "APPROVED_SIMULATED",
                "DECLINED",
                name="insuranceapplicationstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("decision_reason", sa.String(length=200), nullable=False),
        sa.Column("quote_version", sa.String(length=10), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'DECLINED') = (annual_premium_minor IS NULL)",
            name=op.f("ck_insurance_applications_premium_unless_declined"),
        ),
        sa.CheckConstraint(
            "age_band IN ('18-25', '26-35', '36-50', '51-59', '60+')",
            name=op.f("ck_insurance_applications_age_band_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('SUBMITTED', 'APPROVED_SIMULATED', 'DECLINED')",
            name=op.f("ck_insurance_applications_status_valid"),
        ),
        sa.CheckConstraint(
            "annual_premium_minor IS NULL OR annual_premium_minor > 0",
            name=op.f("ck_insurance_applications_premium_positive"),
        ),
        sa.CheckConstraint(
            "cover_minor > 0", name=op.f("ck_insurance_applications_cover_positive")
        ),
        sa.CheckConstraint("term_years > 0", name=op.f("ck_insurance_applications_term_positive")),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_insurance_applications_customer_id_customers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["insurance_products.id"],
            name=op.f("fk_insurance_applications_product_id_insurance_products"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_insurance_applications_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_insurance_applications")),
    )
    op.create_index(
        "ix_insurance_applications_customer_id_created_at",
        "insurance_applications",
        ["customer_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_insurance_applications_product_id",
        "insurance_applications",
        ["product_id"],
        unique=False,
    )
    op.create_index(
        "ix_insurance_applications_user_id", "insurance_applications", ["user_id"], unique=False
    )
    op.create_table(
        "investment_holdings",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("units_micro", sa.BigInteger(), nullable=False),
        sa.Column("cost_basis_minor", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(units_micro = 0) = (cost_basis_minor = 0)",
            name=op.f("ck_investment_holdings_cost_basis_only_with_units"),
        ),
        sa.CheckConstraint(
            "cost_basis_minor >= 0", name=op.f("ck_investment_holdings_cost_basis_non_negative")
        ),
        sa.CheckConstraint(
            "units_micro >= 0", name=op.f("ck_investment_holdings_units_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_investment_holdings_customer_id_customers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["investment_instruments.id"],
            name=op.f("fk_investment_holdings_instrument_id_investment_instruments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investment_holdings")),
        sa.UniqueConstraint(
            "customer_id",
            "instrument_id",
            name=op.f("uq_investment_holdings_customer_id_instrument_id"),
        ),
    )
    op.create_index(
        "ix_investment_holdings_instrument_id",
        "investment_holdings",
        ["instrument_id"],
        unique=False,
    )
    op.create_table(
        "investment_orders",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("holding_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("sip_id", sa.Uuid(), nullable=True),
        sa.Column(
            "side",
            sa.Enum("BUY", "SELL", name="orderside", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("units_micro", sa.BigInteger(), nullable=False),
        sa.Column("price_minor", sa.BigInteger(), nullable=False),
        sa.Column("price_date", sa.Date(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("cost_basis_minor", sa.BigInteger(), nullable=False),
        sa.Column("realised_gain_minor", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(side = 'SELL') = (realised_gain_minor IS NOT NULL)",
            name=op.f("ck_investment_orders_realised_gain_only_on_sale"),
        ),
        sa.CheckConstraint(
            "side = 'SELL' OR cost_basis_minor = amount_minor",
            name=op.f("ck_investment_orders_buy_cost_is_its_amount"),
        ),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name=op.f("ck_investment_orders_side_valid")),
        sa.CheckConstraint(
            "sip_id IS NULL OR side = 'BUY'", name=op.f("ck_investment_orders_sip_only_buys")
        ),
        sa.CheckConstraint("amount_minor > 0", name=op.f("ck_investment_orders_amount_positive")),
        sa.CheckConstraint(
            "cost_basis_minor >= 0", name=op.f("ck_investment_orders_cost_basis_non_negative")
        ),
        sa.CheckConstraint("price_minor > 0", name=op.f("ck_investment_orders_price_positive")),
        sa.CheckConstraint("units_micro > 0", name=op.f("ck_investment_orders_units_positive")),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_investment_orders_account_id_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_investment_orders_customer_id_customers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["holding_id"],
            ["investment_holdings.id"],
            name=op.f("fk_investment_orders_holding_id_investment_holdings"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["investment_instruments.id"],
            name=op.f("fk_investment_orders_instrument_id_investment_instruments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["sip_id"],
            ["fund_sips.id"],
            name=op.f("fk_investment_orders_sip_id_fund_sips"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name=op.f("fk_investment_orders_transaction_id_transactions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_investment_orders_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investment_orders")),
        sa.UniqueConstraint("transaction_id", name=op.f("uq_investment_orders_transaction_id")),
    )
    op.create_index(
        "ix_investment_orders_account_id", "investment_orders", ["account_id"], unique=False
    )
    op.create_index(
        "ix_investment_orders_customer_id_created_at",
        "investment_orders",
        ["customer_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_investment_orders_holding_id", "investment_orders", ["holding_id"], unique=False
    )
    op.create_index(
        "ix_investment_orders_instrument_id", "investment_orders", ["instrument_id"], unique=False
    )
    op.create_index("ix_investment_orders_sip_id", "investment_orders", ["sip_id"], unique=False)
    op.create_index("ix_investment_orders_user_id", "investment_orders", ["user_id"], unique=False)
    op.create_table(
        "fund_sip_runs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("sip_id", sa.Uuid(), nullable=False),
        sa.Column("run_on", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("EXECUTED", "FAILED", name="siprunstatus", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("failure_reason", sa.String(length=40), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'EXECUTED') = (order_id IS NOT NULL)",
            name=op.f("ck_fund_sip_runs_executed_has_order"),
        ),
        sa.CheckConstraint(
            "(status = 'FAILED') = (failure_reason IS NOT NULL)",
            name=op.f("ck_fund_sip_runs_failed_has_reason"),
        ),
        sa.CheckConstraint(
            "status IN ('EXECUTED', 'FAILED')", name=op.f("ck_fund_sip_runs_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["investment_orders.id"],
            name=op.f("fk_fund_sip_runs_order_id_investment_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["sip_id"],
            ["fund_sips.id"],
            name=op.f("fk_fund_sip_runs_sip_id_fund_sips"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fund_sip_runs")),
        sa.UniqueConstraint("order_id", name=op.f("uq_fund_sip_runs_order_id")),
        sa.UniqueConstraint("sip_id", "run_on", name=op.f("uq_fund_sip_runs_sip_id_run_on")),
    )
    _seed()
    _set_fund_notes(FUND_NOTE_AFTER, CATALOGUE_VERSION)
    _replace_type_check(_TYPES_AFTER)


def downgrade() -> None:
    _replace_type_check(_TYPES_BEFORE)  # fails (and rolls back) if investment postings exist
    _set_fund_notes(FUND_NOTE_BEFORE, "2026-11-v1")
    op.drop_table("fund_sip_runs")
    op.drop_index("ix_investment_orders_user_id", table_name="investment_orders")
    op.drop_index("ix_investment_orders_sip_id", table_name="investment_orders")
    op.drop_index("ix_investment_orders_instrument_id", table_name="investment_orders")
    op.drop_index("ix_investment_orders_holding_id", table_name="investment_orders")
    op.drop_index("ix_investment_orders_customer_id_created_at", table_name="investment_orders")
    op.drop_index("ix_investment_orders_account_id", table_name="investment_orders")
    op.drop_table("investment_orders")
    op.drop_index("ix_investment_holdings_instrument_id", table_name="investment_holdings")
    op.drop_table("investment_holdings")
    op.drop_index("ix_insurance_applications_user_id", table_name="insurance_applications")
    op.drop_index("ix_insurance_applications_product_id", table_name="insurance_applications")
    op.drop_index(
        "ix_insurance_applications_customer_id_created_at", table_name="insurance_applications"
    )
    op.drop_table("insurance_applications")
    op.drop_index("ix_fund_sips_user_id", table_name="fund_sips")
    op.drop_index("ix_fund_sips_status_next_run_on", table_name="fund_sips")
    op.drop_index("ix_fund_sips_instrument_id", table_name="fund_sips")
    op.drop_index("ix_fund_sips_customer_id_created_at", table_name="fund_sips")
    op.drop_index("ix_fund_sips_account_id", table_name="fund_sips")
    op.drop_table("fund_sips")
    op.drop_index(
        "ix_investment_instruments_wealth_product_id", table_name="investment_instruments"
    )
    op.drop_table("investment_instruments")
    # Refused by the entries' foreign key if the account was ever posted to (history is kept).
    op.execute(f"DELETE FROM accounts WHERE id = '{SYSTEM_INVESTMENTS[0]}'")
