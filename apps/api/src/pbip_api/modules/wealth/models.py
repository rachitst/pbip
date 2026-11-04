"""Wealth catalogue and fixed deposits (TASK-063, TASK-064, ADR-0025).

``wealth_products`` is reference data seeded by migration 0018 (identical in every environment; a
rate change is a new migration). A fixed deposit stores a SNAPSHOT of the terms it was booked on, so
a later catalogue change never rewrites an existing contract. "Matured" is derived (ACTIVE and the
maturity date reached), never stored, so it cannot go stale.
"""

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from pbip_api.core.db import Base, created_at_col, in_check, str_enum, uuid_pk


class ProductType(StrEnum):
    FIXED_DEPOSIT = "FIXED_DEPOSIT"
    MUTUAL_FUND = "MUTUAL_FUND"
    SOVEREIGN_GOLD_BOND = "SOVEREIGN_GOLD_BOND"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


class YieldBasis(StrEnum):
    GUARANTEED = "GUARANTEED"  # contract rate, compounded quarterly
    FIXED_COUPON = "FIXED_COUPON"  # coupon paid out; the price of the asset also moves
    MARKET_LINKED = "MARKET_LINKED"  # no yield is stated: PBIP has no market data


class FixedDepositStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


class ClosureKind(StrEnum):
    MATURED = "MATURED"  # closed on or after the maturity date: the projected amount is paid
    PREMATURE = "PREMATURE"  # closed early: penalty rate for the time held


class WealthProduct(Base):
    __tablename__ = "wealth_products"
    __table_args__ = (
        in_check("product_type", ProductType, "product_type_valid"),
        in_check("risk_level", RiskLevel, "risk_level_valid"),
        in_check("yield_basis", YieldBasis, "yield_basis_valid"),
        CheckConstraint("annual_rate_bps IS NULL OR annual_rate_bps >= 0", name="rate_valid"),
        CheckConstraint(
            "(yield_basis = 'MARKET_LINKED') = (annual_rate_bps IS NULL)",
            name="rate_only_when_not_market_linked",
        ),
        CheckConstraint("min_lock_in_months >= 0", name="lock_in_non_negative"),
        CheckConstraint(
            "min_amount_minor IS NULL OR min_amount_minor > 0", name="min_amount_positive"
        ),
        CheckConstraint(
            "max_amount_minor IS NULL OR max_amount_minor >= min_amount_minor",
            name="max_amount_at_least_min",
        ),
        CheckConstraint(
            "premature_penalty_bps IS NULL OR premature_penalty_bps >= 0",
            name="penalty_non_negative",
        ),
        CheckConstraint(
            "NOT bookable OR (product_type = 'FIXED_DEPOSIT' AND annual_rate_bps IS NOT NULL "
            "AND min_amount_minor IS NOT NULL AND max_amount_minor IS NOT NULL "
            "AND cardinality(tenures_months) > 0)",
            name="bookable_is_a_complete_deposit",
        ),
        Index("ix_wealth_products_risk_level", "risk_level"),
    )

    id: Mapped[UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    product_type: Mapped[ProductType] = mapped_column(str_enum(ProductType), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    risk_level: Mapped[RiskLevel] = mapped_column(str_enum(RiskLevel), nullable=False)
    yield_basis: Mapped[YieldBasis] = mapped_column(str_enum(YieldBasis), nullable=False)
    annual_rate_bps: Mapped[int | None] = mapped_column(Integer)
    tenures_months: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False)
    min_lock_in_months: Mapped[int] = mapped_column(Integer, nullable=False)
    min_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    max_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    premature_penalty_bps: Mapped[int | None] = mapped_column(Integer)
    bookable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    notes: Mapped[str] = mapped_column(String(300), nullable=False)
    catalogue_version: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class FixedDeposit(Base):
    __tablename__ = "fixed_deposits"
    __table_args__ = (
        in_check("status", FixedDepositStatus, "status_valid"),
        in_check("closure_kind", ClosureKind, "closure_kind_valid"),
        CheckConstraint("principal > 0", name="principal_positive"),
        CheckConstraint("annual_rate_bps >= 0", name="rate_non_negative"),
        CheckConstraint(
            "tenure_months > 0 AND mod(tenure_months, 3) = 0", name="tenure_in_quarters"
        ),
        CheckConstraint("lock_in_months >= 0", name="lock_in_non_negative"),
        CheckConstraint("maturity_interest >= 0", name="maturity_interest_non_negative"),
        CheckConstraint(
            "maturity_amount = principal + maturity_interest",
            name="maturity_amount_is_principal_plus_interest",
        ),
        CheckConstraint("maturity_date > start_date", name="matures_after_start"),
        CheckConstraint(
            "premature_penalty_bps IS NULL OR premature_penalty_bps >= 0",
            name="penalty_non_negative",
        ),
        CheckConstraint(
            "(status = 'CLOSED') = (closed_at IS NOT NULL AND closed_on IS NOT NULL "
            "AND closure_kind IS NOT NULL AND closure_transaction_id IS NOT NULL "
            "AND paid_interest IS NOT NULL)",
            name="closed_has_payout",
        ),
        CheckConstraint(
            "paid_interest IS NULL OR paid_interest >= 0", name="paid_interest_non_negative"
        ),
        CheckConstraint(
            "interest_transaction_id IS NULL OR paid_interest > 0",
            name="interest_transaction_only_for_interest",
        ),
        Index("ix_fixed_deposits_customer_id_created_at", "customer_id", "created_at"),
        Index("ix_fixed_deposits_user_id", "user_id"),
        Index("ix_fixed_deposits_account_id", "account_id"),
        Index("ix_fixed_deposits_product_id", "product_id"),
        # ADR-0029: a payout can only be held while the deposit is still open.
        CheckConstraint(
            "payout_held_at IS NULL OR status = 'ACTIVE'", name="payout_held_only_while_open"
        ),
        # ADR-0029: the hourly maturity payout poll (status = ACTIVE AND maturity_date <= today).
        Index("ix_fixed_deposits_status_maturity_date", "status", "maturity_date"),
    )

    id: Mapped[UUID] = uuid_pk()
    customer_id: Mapped[UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )  # funded the deposit and receives the payout
    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("wealth_products.id", ondelete="RESTRICT"), nullable=False
    )
    booking_transaction_id: Mapped[UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    closure_transaction_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT"), unique=True
    )
    interest_transaction_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT"), unique=True
    )
    annual_rate_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    tenure_months: Mapped[int] = mapped_column(Integer, nullable=False)
    lock_in_months: Mapped[int] = mapped_column(Integer, nullable=False)
    premature_penalty_bps: Mapped[int | None] = mapped_column(Integer)
    principal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    maturity_interest: Mapped[int] = mapped_column(BigInteger, nullable=False)
    maturity_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    maturity_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[FixedDepositStatus] = mapped_column(
        str_enum(FixedDepositStatus), default=FixedDepositStatus.ACTIVE, nullable=False
    )
    closure_kind: Mapped[ClosureKind | None] = mapped_column(str_enum(ClosureKind))
    paid_interest: Mapped[int | None] = mapped_column(BigInteger)
    closed_on: Mapped[date | None] = mapped_column(Date)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the automatic maturity payout first found the account not active (ADR-0029); the
    # customer is notified once per hold, and the payout is retried every run until it succeeds.
    payout_held_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class InsuranceType(StrEnum):
    TERM_LIFE = "TERM_LIFE"
    HEALTH = "HEALTH"


class InsuranceProduct(Base):
    """Insurance catalogue (TASK-096, ADR-0028): reference data seeded by migration 0020.

    A separate table from ``wealth_products`` because insurance is protection, not investment: it
    has a cover range, entry ages and a premium the customer pays, and no yield, lock-in or risk
    level. The premium is ONE indicative figure for the stated reference cover, age and term; PBIP
    does no underwriting, so nothing here is a quote for a particular customer.
    """

    __tablename__ = "insurance_products"
    __table_args__ = (
        in_check("insurance_type", InsuranceType, "insurance_type_valid"),
        CheckConstraint("min_cover_minor > 0", name="min_cover_positive"),
        CheckConstraint("max_cover_minor >= min_cover_minor", name="max_cover_at_least_min"),
        CheckConstraint(
            "min_entry_age >= 18 AND max_entry_age >= min_entry_age AND max_entry_age <= 120",
            name="entry_ages_valid",
        ),
        CheckConstraint(
            "max_cover_age IS NULL OR max_cover_age > max_entry_age", name="max_cover_age_valid"
        ),
        CheckConstraint(
            "cardinality(policy_terms_years) > 0 AND 0 < ALL(policy_terms_years)",
            name="policy_terms_positive",
        ),
        CheckConstraint(
            "reference_cover_minor BETWEEN min_cover_minor AND max_cover_minor",
            name="reference_cover_in_range",
        ),
        CheckConstraint(
            "reference_age BETWEEN min_entry_age AND max_entry_age", name="reference_age_eligible"
        ),
        CheckConstraint(
            "reference_term_years = ANY(policy_terms_years)", name="reference_term_offered"
        ),
        CheckConstraint(
            "max_cover_age IS NULL OR reference_age + reference_term_years <= max_cover_age",
            name="reference_term_within_cover_age",
        ),
        CheckConstraint("indicative_annual_premium_minor > 0", name="indicative_premium_positive"),
        Index("ix_insurance_products_insurance_type", "insurance_type"),
    )

    id: Mapped[UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    insurance_type: Mapped[InsuranceType] = mapped_column(str_enum(InsuranceType), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    min_cover_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_cover_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    min_entry_age: Mapped[int] = mapped_column(Integer, nullable=False)
    max_entry_age: Mapped[int] = mapped_column(Integer, nullable=False)
    max_cover_age: Mapped[int | None] = mapped_column(Integer)  # None: renewable for life
    policy_terms_years: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False)
    reference_cover_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reference_age: Mapped[int] = mapped_column(Integer, nullable=False)
    reference_term_years: Mapped[int] = mapped_column(Integer, nullable=False)
    indicative_annual_premium_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    notes: Mapped[str] = mapped_column(String(300), nullable=False)
    catalogue_version: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = created_at_col()


# --------------------------------------------------------------------------------- investing
# ADR-0058. Money is integer paise; units are integer MICRO-units (1 unit = 1,000,000), never a
# float. Prices come from ``wealth/pricing.py``: a deterministic SIMULATED source, not market data.


class InstrumentKind(StrEnum):
    MUTUAL_FUND = "MUTUAL_FUND"  # bought by amount
    STOCK = "STOCK"  # bought and sold in whole units
    BOND = "BOND"  # bought and sold in whole units; accumulating (no coupon is paid out)


class InvestmentInstrument(Base):
    """What can be bought (ADR-0058 section 1): reference data seeded by migration 0031."""

    __tablename__ = "investment_instruments"
    __table_args__ = (
        in_check("kind", InstrumentKind, "kind_valid"),
        in_check("risk_level", RiskLevel, "risk_level_valid"),
        CheckConstraint(
            "(kind = 'MUTUAL_FUND') = (wealth_product_id IS NOT NULL)",
            name="fund_links_its_catalogue_row",
        ),
        CheckConstraint(
            "(kind = 'MUTUAL_FUND') = (min_order_minor IS NOT NULL)",
            name="min_order_only_for_funds",
        ),
        CheckConstraint(
            "min_order_minor IS NULL OR min_order_minor > 0", name="min_order_positive"
        ),
        CheckConstraint("price_base_minor > 0", name="price_base_positive"),
        CheckConstraint("price_drift_bps BETWEEN -5000 AND 5000", name="price_drift_bounded"),
        CheckConstraint("price_wobble_bps BETWEEN 0 AND 2000", name="price_wobble_bounded"),
        Index("ix_investment_instruments_wealth_product_id", "wealth_product_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    kind: Mapped[InstrumentKind] = mapped_column(str_enum(InstrumentKind), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    risk_level: Mapped[RiskLevel] = mapped_column(str_enum(RiskLevel), nullable=False)
    wealth_product_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("wealth_products.id", ondelete="RESTRICT")
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    min_order_minor: Mapped[int | None] = mapped_column(BigInteger)
    price_base_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    price_drift_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    price_wobble_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    notes: Mapped[str] = mapped_column(String(300), nullable=False)
    catalogue_version: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class InvestmentHolding(Base):
    """A customer's position in one instrument (average cost basis). Only ``wealth/service.py``
    changes it, inside the same transaction as the ledger posting that paid for it."""

    __tablename__ = "investment_holdings"
    __table_args__ = (
        UniqueConstraint("customer_id", "instrument_id"),
        CheckConstraint("units_micro >= 0", name="units_non_negative"),
        CheckConstraint("cost_basis_minor >= 0", name="cost_basis_non_negative"),
        CheckConstraint(
            "(units_micro = 0) = (cost_basis_minor = 0)", name="cost_basis_only_with_units"
        ),
        Index("ix_investment_holdings_instrument_id", "instrument_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    customer_id: Mapped[UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    instrument_id: Mapped[UUID] = mapped_column(
        ForeignKey("investment_instruments.id", ondelete="RESTRICT"), nullable=False
    )
    units_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cost_basis_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = created_at_col()


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class InvestmentOrder(Base):
    """One executed order; never updated. The ledger transaction is the financial truth."""

    __tablename__ = "investment_orders"
    __table_args__ = (
        in_check("side", OrderSide, "side_valid"),
        CheckConstraint("units_micro > 0", name="units_positive"),
        CheckConstraint("price_minor > 0", name="price_positive"),
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint("cost_basis_minor >= 0", name="cost_basis_non_negative"),
        CheckConstraint(
            "(side = 'SELL') = (realised_gain_minor IS NOT NULL)", name="realised_gain_only_on_sale"
        ),
        CheckConstraint(
            "side = 'SELL' OR cost_basis_minor = amount_minor", name="buy_cost_is_its_amount"
        ),
        CheckConstraint("sip_id IS NULL OR side = 'BUY'", name="sip_only_buys"),
        Index("ix_investment_orders_customer_id_created_at", "customer_id", "created_at"),
        Index("ix_investment_orders_user_id", "user_id"),
        Index("ix_investment_orders_holding_id", "holding_id"),
        Index("ix_investment_orders_instrument_id", "instrument_id"),
        Index("ix_investment_orders_account_id", "account_id"),
        Index("ix_investment_orders_sip_id", "sip_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    customer_id: Mapped[UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    holding_id: Mapped[UUID] = mapped_column(
        ForeignKey("investment_holdings.id", ondelete="RESTRICT"), nullable=False
    )
    instrument_id: Mapped[UUID] = mapped_column(
        ForeignKey("investment_instruments.id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    transaction_id: Mapped[UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    sip_id: Mapped[UUID | None] = mapped_column(ForeignKey("fund_sips.id", ondelete="RESTRICT"))
    side: Mapped[OrderSide] = mapped_column(str_enum(OrderSide), nullable=False)
    units_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)
    price_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    price_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cost_basis_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    realised_gain_minor: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = created_at_col()


class SipStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"


class FundSip(Base):
    """A monthly fund purchase (ADR-0058 section 7), run by the ``run-fund-sips`` beat job."""

    __tablename__ = "fund_sips"
    __table_args__ = (
        in_check("status", SipStatus, "status_valid"),
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint("day_of_month BETWEEN 1 AND 28", name="day_of_month_valid"),
        CheckConstraint(
            "(status = 'CANCELLED') = (cancelled_at IS NOT NULL)", name="cancelled_has_time"
        ),
        Index("ix_fund_sips_status_next_run_on", "status", "next_run_on"),
        Index("ix_fund_sips_customer_id_created_at", "customer_id", "created_at"),
        Index("ix_fund_sips_user_id", "user_id"),
        Index("ix_fund_sips_account_id", "account_id"),
        Index("ix_fund_sips_instrument_id", "instrument_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    customer_id: Mapped[UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    instrument_id: Mapped[UUID] = mapped_column(
        ForeignKey("investment_instruments.id", ondelete="RESTRICT"), nullable=False
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    day_of_month: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[SipStatus] = mapped_column(
        str_enum(SipStatus), default=SipStatus.ACTIVE, nullable=False
    )
    next_run_on: Mapped[date] = mapped_column(Date, nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class SipRunStatus(StrEnum):
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"


class FundSipRun(Base):
    """One instalment attempt. ``UNIQUE (sip_id, run_on)`` makes a run exactly-once for good."""

    __tablename__ = "fund_sip_runs"
    __table_args__ = (
        UniqueConstraint("sip_id", "run_on"),
        in_check("status", SipRunStatus, "status_valid"),
        CheckConstraint(
            "(status = 'EXECUTED') = (order_id IS NOT NULL)", name="executed_has_order"
        ),
        CheckConstraint(
            "(status = 'FAILED') = (failure_reason IS NOT NULL)", name="failed_has_reason"
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    sip_id: Mapped[UUID] = mapped_column(
        ForeignKey("fund_sips.id", ondelete="RESTRICT"), nullable=False
    )
    run_on: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[SipRunStatus] = mapped_column(str_enum(SipRunStatus), nullable=False)
    order_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("investment_orders.id", ondelete="RESTRICT"), unique=True
    )
    failure_reason: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = created_at_col()


class InsuranceApplicationStatus(StrEnum):
    SUBMITTED = "SUBMITTED"  # referred: an insurer would need the exact age or medicals
    APPROVED_SIMULATED = "APPROVED_SIMULATED"  # meets the simulated rules; no policy is issued
    DECLINED = "DECLINED"


class InsuranceApplication(Base):
    """A simulated insurance application (ADR-0058 section 8). No premium is ever debited."""

    __tablename__ = "insurance_applications"
    __table_args__ = (
        in_check("status", InsuranceApplicationStatus, "status_valid"),
        CheckConstraint(
            "age_band IN ('18-25', '26-35', '36-50', '51-59', '60+')", name="age_band_valid"
        ),
        CheckConstraint("cover_minor > 0", name="cover_positive"),
        CheckConstraint("term_years > 0", name="term_positive"),
        CheckConstraint(
            "(status = 'DECLINED') = (annual_premium_minor IS NULL)",
            name="premium_unless_declined",
        ),
        CheckConstraint(
            "annual_premium_minor IS NULL OR annual_premium_minor > 0", name="premium_positive"
        ),
        Index("ix_insurance_applications_customer_id_created_at", "customer_id", "created_at"),
        Index("ix_insurance_applications_user_id", "user_id"),
        Index("ix_insurance_applications_product_id", "product_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    customer_id: Mapped[UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("insurance_products.id", ondelete="RESTRICT"), nullable=False
    )
    cover_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    term_years: Mapped[int] = mapped_column(Integer, nullable=False)
    age_band: Mapped[str] = mapped_column(String(8), nullable=False)
    annual_premium_minor: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[InsuranceApplicationStatus] = mapped_column(
        str_enum(InsuranceApplicationStatus), nullable=False
    )
    decision_reason: Mapped[str] = mapped_column(String(200), nullable=False)
    quote_version: Mapped[str] = mapped_column(String(10), nullable=False)
    created_at: Mapped[datetime] = created_at_col()
