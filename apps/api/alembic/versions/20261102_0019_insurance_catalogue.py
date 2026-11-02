"""Insurance catalogue: term-life and health products with indicative premiums (TASK-096).

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-02 18:00:00.000000+00:00

ADR-0028. ``insurance_products`` is reference data seeded here, identical in every environment: a
premium or limit change is a NEW migration, never an edit of this one. Each premium is PBIP's own
indicative, simulated figure for the stated reference cover, age and term (a non-smoker), of the
order of Indian retail premiums but not copied from any insurer. PBIP does no underwriting. Nothing
references this table, so the downgrade drops it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATALOGUE_VERSION = "2026-11-v1"
LAKH = 1_00_000_00  # INR 1 lakh in paise
CRORE = 100 * LAKH
_TERM_NOTE = (
    "Indicative, simulated premium for the reference profile shown (non-smoker), not an insurer's "
    "quote. PBIP does no underwriting; the insurer sets the premium. Not sold online by PBIP."
)
_HEALTH_NOTE = (
    "Indicative, simulated premium for the reference profile shown, not an insurer's quote. "
    "Waiting periods for existing illnesses apply. PBIP does no underwriting. "
    "Not sold online by PBIP."
)


def _product(
    n: int,
    code: str,
    insurance_type: str,
    name: str,
    description: str,
    cover: tuple[int, int],
    entry_ages: tuple[int, int],
    max_cover_age: int | None,
    terms: list[int],
    reference: tuple[int, int, int, int],
    notes: str,
) -> dict[str, object]:
    ref_cover, ref_age, ref_term, premium = reference
    return {
        "id": f"30000000-0000-4000-a000-{n:012d}",
        "code": code,
        "insurance_type": insurance_type,
        "name": name,
        "description": description,
        "min_cover_minor": cover[0],
        "max_cover_minor": cover[1],
        "min_entry_age": entry_ages[0],
        "max_entry_age": entry_ages[1],
        "max_cover_age": max_cover_age,
        "policy_terms_years": terms,
        "reference_cover_minor": ref_cover,
        "reference_age": ref_age,
        "reference_term_years": ref_term,
        "indicative_annual_premium_minor": premium,
        "notes": notes,
        "catalogue_version": CATALOGUE_VERSION,
    }


PRODUCTS = [
    _product(1, "TERM-LIFE-CLASSIC", "TERM_LIFE", "Term life cover (illustrative)",
             "Pure protection: pays the cover to your nominee if you die during the term. "
             "No maturity or savings value.",
             (25 * LAKH, 5 * CRORE), (18, 60), 75, [10, 15, 20, 25, 30, 35, 40],
             (1 * CRORE, 30, 30, 12_500_00), _TERM_NOTE),
    _product(2, "TERM-LIFE-SMALL", "TERM_LIFE", "Term life cover, smaller sums (illustrative)",
             "Pure protection for smaller covers and shorter terms; entry up to age 65.",
             (10 * LAKH, 50 * LAKH), (18, 65), 75, [5, 10, 15, 20],
             (25 * LAKH, 40, 20, 7_200_00), _TERM_NOTE),
    _product(3, "HEALTH-INDIVIDUAL", "HEALTH", "Individual health cover (illustrative)",
             "Pays hospital bills up to the sum insured each policy year; renewable every year "
             "for life.",
             (3 * LAKH, 1 * CRORE), (18, 65), None, [1],
             (5 * LAKH, 30, 1, 7_800_00), _HEALTH_NOTE),
    _product(4, "HEALTH-SENIOR", "HEALTH", "Senior citizen health cover (illustrative)",
             "Health cover for people aged 60 to 75 at entry; renewable every year for life.",
             (3 * LAKH, 25 * LAKH), (60, 75), None, [1],
             (5 * LAKH, 65, 1, 32_000_00), _HEALTH_NOTE),
]  # fmt: skip


def _seed() -> None:
    products = sa.table(
        "insurance_products",
        sa.column("id", sa.Uuid()),
        sa.column("code", sa.String),
        sa.column("insurance_type", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.String),
        sa.column("min_cover_minor", sa.BigInteger),
        sa.column("max_cover_minor", sa.BigInteger),
        sa.column("min_entry_age", sa.Integer),
        sa.column("max_entry_age", sa.Integer),
        sa.column("max_cover_age", sa.Integer),
        sa.column("policy_terms_years", postgresql.ARRAY(sa.Integer())),
        sa.column("reference_cover_minor", sa.BigInteger),
        sa.column("reference_age", sa.Integer),
        sa.column("reference_term_years", sa.Integer),
        sa.column("indicative_annual_premium_minor", sa.BigInteger),
        sa.column("notes", sa.String),
        sa.column("catalogue_version", sa.String),
    )
    op.bulk_insert(products, PRODUCTS)


def upgrade() -> None:
    op.create_table(
        "insurance_products",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column(
            "insurance_type",
            sa.Enum("TERM_LIFE", "HEALTH", name="insurancetype", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=False),
        sa.Column("min_cover_minor", sa.BigInteger(), nullable=False),
        sa.Column("max_cover_minor", sa.BigInteger(), nullable=False),
        sa.Column("min_entry_age", sa.Integer(), nullable=False),
        sa.Column("max_entry_age", sa.Integer(), nullable=False),
        sa.Column("max_cover_age", sa.Integer(), nullable=True),
        sa.Column("policy_terms_years", postgresql.ARRAY(sa.Integer()), nullable=False),
        sa.Column("reference_cover_minor", sa.BigInteger(), nullable=False),
        sa.Column("reference_age", sa.Integer(), nullable=False),
        sa.Column("reference_term_years", sa.Integer(), nullable=False),
        sa.Column("indicative_annual_premium_minor", sa.BigInteger(), nullable=False),
        sa.Column("notes", sa.String(length=300), nullable=False),
        sa.Column("catalogue_version", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "insurance_type IN ('TERM_LIFE', 'HEALTH')",
            name=op.f("ck_insurance_products_insurance_type_valid"),
        ),
        sa.CheckConstraint(
            "min_cover_minor > 0", name=op.f("ck_insurance_products_min_cover_positive")
        ),
        sa.CheckConstraint(
            "max_cover_minor >= min_cover_minor",
            name=op.f("ck_insurance_products_max_cover_at_least_min"),
        ),
        sa.CheckConstraint(
            "min_entry_age >= 18 AND max_entry_age >= min_entry_age AND max_entry_age <= 120",
            name=op.f("ck_insurance_products_entry_ages_valid"),
        ),
        sa.CheckConstraint(
            "max_cover_age IS NULL OR max_cover_age > max_entry_age",
            name=op.f("ck_insurance_products_max_cover_age_valid"),
        ),
        sa.CheckConstraint(
            "cardinality(policy_terms_years) > 0 AND 0 < ALL(policy_terms_years)",
            name=op.f("ck_insurance_products_policy_terms_positive"),
        ),
        sa.CheckConstraint(
            "reference_cover_minor BETWEEN min_cover_minor AND max_cover_minor",
            name=op.f("ck_insurance_products_reference_cover_in_range"),
        ),
        sa.CheckConstraint(
            "reference_age BETWEEN min_entry_age AND max_entry_age",
            name=op.f("ck_insurance_products_reference_age_eligible"),
        ),
        sa.CheckConstraint(
            "reference_term_years = ANY(policy_terms_years)",
            name=op.f("ck_insurance_products_reference_term_offered"),
        ),
        sa.CheckConstraint(
            "max_cover_age IS NULL OR reference_age + reference_term_years <= max_cover_age",
            name=op.f("ck_insurance_products_reference_term_within_cover_age"),
        ),
        sa.CheckConstraint(
            "indicative_annual_premium_minor > 0",
            name=op.f("ck_insurance_products_indicative_premium_positive"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_insurance_products")),
        sa.UniqueConstraint("code", name=op.f("uq_insurance_products_code")),
    )
    op.create_index(
        "ix_insurance_products_insurance_type",
        "insurance_products",
        ["insurance_type"],
        unique=False,
    )
    _seed()


def downgrade() -> None:
    op.drop_index("ix_insurance_products_insurance_type", table_name="insurance_products")
    op.drop_table("insurance_products")
