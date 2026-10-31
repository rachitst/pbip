"""Bill payments schema: billers and bill_payments tables (TASK-014)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-31 11:40:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "billers",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "category",
            sa.Enum(
                "ELECTRICITY",
                "WATER",
                "TELECOM",
                "GAS",
                "BROADBAND",
                "INSURANCE",
                name="billercategory",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("biller_code", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "ACTIVE",
                "INACTIVE",
                name="billerstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "category IN ('ELECTRICITY', 'WATER', 'TELECOM', 'GAS', 'BROADBAND', 'INSURANCE')",
            name=op.f("ck_billers_category_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'INACTIVE')",
            name=op.f("ck_billers_status_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billers")),
        sa.UniqueConstraint("biller_code", name=op.f("uq_billers_biller_code")),
    )
    op.create_index("ix_billers_status", "billers", ["status"], unique=False)

    op.create_table(
        "bill_payments",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("biller_id", sa.Uuid(), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("consumer_reference", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "POSTED",
                "DECLINED",
                "REVERSED",
                name="billpaymentstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('POSTED', 'DECLINED', 'REVERSED')",
            name=op.f("ck_bill_payments_status_valid"),
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_bill_payments_amount_positive")),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_bill_payments_account_id_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["biller_id"],
            ["billers.id"],
            name=op.f("fk_bill_payments_biller_id_billers"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_bill_payments_customer_id_customers"),
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name=op.f("fk_bill_payments_transaction_id_transactions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bill_payments")),
        sa.UniqueConstraint("transaction_id", name=op.f("uq_bill_payments_transaction_id")),
    )
    op.create_index(
        "ix_bill_payments_biller_id", "bill_payments", ["biller_id"], unique=False
    )
    op.create_index(
        "ix_bill_payments_customer_id_created_at",
        "bill_payments",
        ["customer_id", "created_at"],
        unique=False,
    )

    # Expand transactions.type CHECK constraint to include BILL_PAYMENT.
    op.drop_constraint(
        op.f("ck_transactions_type_valid"), "transactions", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_transactions_type_valid"),
        "transactions",
        "type IN ('TRANSFER', 'DEPOSIT', 'REVERSAL', 'FEE', 'BILL_PAYMENT')",
    )

    # Seed reference utility billers so a freshly migrated environment has active billers.
    op.execute(
        """
        INSERT INTO billers (id, name, category, biller_code, status) VALUES
          ('00000000-0000-0000-0000-0000000000b1', 'Maharashtra State Electricity Board', 'ELECTRICITY', 'MSEB-MH', 'ACTIVE'),
          ('00000000-0000-0000-0000-0000000000b2', 'Brihanmumbai Municipal Water Supply', 'WATER', 'BMC-WTR', 'ACTIVE'),
          ('00000000-0000-0000-0000-0000000000b3', 'JioFiber Broadband Services', 'BROADBAND', 'JIO-FBR', 'ACTIVE'),
          ('00000000-0000-0000-0000-0000000000b4', 'Mahanagar Gas Limited', 'GAS', 'MGL-GAS', 'ACTIVE'),
          ('00000000-0000-0000-0000-0000000000b5', 'Airtel Postpaid Mobile', 'TELECOM', 'AIRTEL-POST', 'ACTIVE')
        ON CONFLICT (biller_code) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_transactions_type_valid"), "transactions", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_transactions_type_valid"),
        "transactions",
        "type IN ('TRANSFER', 'DEPOSIT', 'REVERSAL', 'FEE')",
    )

    op.drop_index("ix_bill_payments_customer_id_created_at", table_name="bill_payments")
    op.drop_index("ix_bill_payments_biller_id", table_name="bill_payments")
    op.drop_table("bill_payments")

    op.drop_index("ix_billers_status", table_name="billers")
    op.drop_table("billers")
