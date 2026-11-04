"""Wealth API contracts (TASK-063, TASK-064). Money is integer minor units (paise); rates are
basis points (710 = 7.10%)."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_AMOUNT_MINOR = 10_00_00_000_00  # INR 10 crore: above every catalogue maximum
MAX_ORDER_UNITS = 100_000  # whole stocks or bonds in one order
MAX_UNITS_MICRO = 10**15  # a bound on any sale; far above any holding the order caps allow

RiskLevelLiteral = Literal["LOW", "MODERATE", "HIGH"]


class WealthProductOut(BaseModel):
    id: UUID
    code: str
    product_type: Literal["FIXED_DEPOSIT", "MUTUAL_FUND", "SOVEREIGN_GOLD_BOND"]
    name: str
    description: str
    risk_level: RiskLevelLiteral
    yield_basis: Literal["GUARANTEED", "FIXED_COUPON", "MARKET_LINKED"]
    annual_rate_bps: int | None = Field(
        description="Indicative, simulated annual rate (deposits) or coupon (gold bond); "
        "None when market-linked"
    )
    apy_bps: int | None = Field(
        description="Annualised yield: quarterly compounding for deposits, the coupon for the gold "
        "bond; None for market-linked funds (PBIP has no market data)"
    )
    tenures_months: list[int] = Field(description="Empty for open-ended funds")
    min_lock_in_months: int
    min_amount_minor: int | None
    max_amount_minor: int | None
    premature_closure_allowed: bool
    premature_penalty_bps: int | None
    bookable: bool = Field(description="Only fixed deposits can be booked online")
    investable: bool = Field(
        description="A fund that can be bought online at a simulated price (ADR-0058)"
    )
    notes: str
    catalogue_version: str


class DepositTermsRequest(BaseModel):
    """No rate, maturity or customer from the client: the rate comes from the catalogue, the
    maturity from ``deposit_math`` and the customer from the session (ADR-0025)."""

    model_config = ConfigDict(extra="forbid")

    product_id: UUID
    amount_minor: int = Field(gt=0, le=MAX_AMOUNT_MINOR)
    tenure_months: int = Field(gt=0, le=120)


class DepositQuoteOut(BaseModel):
    product_id: UUID
    product_name: str
    principal_minor: int
    tenure_months: int
    annual_rate_bps: int
    apy_bps: int
    start_date: date
    maturity_date: date
    maturity_interest_minor: int
    maturity_amount_minor: int
    lock_in_months: int
    premature_penalty_bps: int | None


class DepositBookRequest(DepositTermsRequest):
    account_id: UUID = Field(
        description="One of MY accounts: funds the deposit, receives the payout"
    )


class FixedDepositOut(BaseModel):
    id: UUID
    product_id: UUID
    product_name: str
    status: Literal["ACTIVE", "MATURED", "CLOSED"] = Field(
        description="MATURED = active and the maturity date reached: ready to withdraw"
    )
    account_id: UUID
    currency: str
    principal_minor: int
    annual_rate_bps: int
    tenure_months: int
    start_date: date
    maturity_date: date
    days_to_maturity: int = Field(description="Bank days until maturity; 0 once matured")
    maturity_interest_minor: int
    maturity_amount_minor: int
    accrued_interest_minor: int = Field(
        description="Interest earned to today at the contract rate (paid interest once closed)"
    )
    lock_in_until: date
    can_close_now: bool
    payout_if_closed_today_minor: int | None = Field(
        description="What closing today would pay (premature penalty applied); None if it cannot"
    )
    closure_kind: Literal["MATURED", "PREMATURE"] | None
    paid_interest_minor: int | None
    closed_on: date | None
    booking_transaction_id: UUID
    closure_transaction_id: UUID | None
    interest_transaction_id: UUID | None
    booked_at: datetime


class AllocationSlice(BaseModel):
    asset_class: Literal["CASH", "DEPOSITS", "FUNDS", "STOCKS", "BONDS"]
    value_minor: int
    share_bps: int = Field(description="Share of the total; the slices sum to 10000 (0 if empty)")


class PortfolioOut(BaseModel):
    currency: Literal["INR"]
    as_of: date
    cash_minor: int = Field(description="Ledger balance of my INR accounts")
    invested_principal_minor: int = Field(description="Principal of my open fixed deposits")
    accrued_interest_minor: int = Field(description="Interest earned to date on open deposits")
    deposits_value_minor: int = Field(description="Principal + accrued interest")
    projected_maturity_value_minor: int
    funds_minor: int = Field(description="Fund holdings at today's simulated NAV (ADR-0058)")
    stocks_minor: int = Field(description="Stock holdings at today's simulated price")
    bonds_minor: int = Field(description="Bond holdings at today's simulated price")
    investments_cost_minor: int = Field(description="Cost basis of fund, stock and bond holdings")
    investments_gain_minor: int = Field(
        description="Value of fund, stock and bond holdings minus their cost basis (unrealised)"
    )
    total_minor: int
    open_deposits: int
    allocation: list[AllocationSlice]


InsuranceTypeLiteral = Literal["TERM_LIFE", "HEALTH"]


class IndicativePremiumOut(BaseModel):
    """ONE illustrative figure at a stated reference profile, never a quote for this customer
    (PBIP does no underwriting, ADR-0028)."""

    annual_premium_minor: int = Field(description="Indicative, simulated annual premium (paise)")
    reference_cover_minor: int = Field(description="Cover the premium is stated for")
    reference_age: int = Field(description="Age at entry the premium is stated for")
    reference_term_years: int = Field(description="Policy term the premium is stated for")


class AgeBandHintOut(BaseModel):
    age_band: Literal["18-25", "26-35", "36-50", "51-59", "60+"]
    whole_band_eligible: bool = Field(
        description="False when only some ages in the band are inside the entry ages"
    )


class InsuranceEligibilityOut(BaseModel):
    """Hints from the product's own rules only: the customer's data is not read."""

    min_entry_age: int
    max_entry_age: int
    max_cover_age: int | None = Field(
        description="Cover ends at this age; None: renewable for life"
    )
    age_bands: list[AgeBandHintOut] = Field(description="Profile age bands that can apply")


class InsuranceProductOut(BaseModel):
    id: UUID
    code: str
    insurance_type: InsuranceTypeLiteral
    name: str
    description: str
    currency: Literal["INR"]
    cover_min_minor: int = Field(description="Smallest cover (sum insured/assured) offered")
    cover_max_minor: int = Field(description="Largest cover offered")
    policy_terms_years: list[int] = Field(description="Terms offered; [1] for yearly renewal")
    indicative_premium: IndicativePremiumOut
    eligibility: InsuranceEligibilityOut
    notes: str
    catalogue_version: str


# ----------------------------------------------------------------- investing (ADR-0058)
InstrumentKindLiteral = Literal["MUTUAL_FUND", "STOCK", "BOND"]
PRICE_SOURCE_DESCRIPTION = "Always SIMULATED: PBIP's deterministic price model, not market data"


class InstrumentOut(BaseModel):
    id: UUID
    code: str
    kind: InstrumentKindLiteral
    name: str
    description: str
    risk_level: RiskLevelLiteral
    wealth_product_id: UUID | None = Field(description="The fund's catalogue row; None otherwise")
    currency: Literal["INR"]
    order_mode: Literal["AMOUNT", "UNITS"] = Field(
        description="AMOUNT: funds are bought by amount; UNITS: stocks and bonds in whole units"
    )
    min_order_minor: int | None = Field(description="Smallest fund purchase; None for units")
    price_minor: int = Field(description="Price of one unit today, in paise")
    price_date: date = Field(description="The bank day the price is for (one price a day)")
    price_source: Literal["SIMULATED"] = Field(description=PRICE_SOURCE_DESCRIPTION)
    notes: str
    catalogue_version: str


class BuyRequest(BaseModel):
    """Exactly one of ``amount_minor`` (funds) or ``units`` (stocks and bonds). No price from
    the client: the price is the simulated price of the bank day the order executes."""

    model_config = ConfigDict(extra="forbid")

    instrument_id: UUID
    account_id: UUID = Field(description="One of MY accounts: pays for the order")
    amount_minor: int | None = Field(default=None, gt=0, le=MAX_AMOUNT_MINOR)
    units: int | None = Field(default=None, gt=0, le=MAX_ORDER_UNITS)

    @model_validator(mode="after")
    def _one_quantity(self) -> "BuyRequest":
        if (self.amount_minor is None) == (self.units is None):
            raise ValueError("give exactly one of amount_minor or units")
        return self


class SellRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: UUID = Field(description="One of MY accounts: receives the proceeds")
    units_micro: int = Field(
        gt=0,
        le=MAX_UNITS_MICRO,
        description="Micro-units to sell (1 unit = 1,000,000); whole units for stocks and bonds",
    )


class OrderOut(BaseModel):
    id: UUID
    side: Literal["BUY", "SELL"]
    holding_id: UUID
    instrument_id: UUID
    instrument_code: str
    instrument_name: str
    account_id: UUID
    units_micro: int
    price_minor: int
    price_date: date
    price_source: Literal["SIMULATED"] = Field(description=PRICE_SOURCE_DESCRIPTION)
    amount_minor: int = Field(description="Debited (BUY) or credited (SELL), in paise")
    cost_basis_minor: int = Field(description="Cost added (BUY) or taken out (SELL)")
    realised_gain_minor: int | None = Field(description="SELL only: proceeds minus cost taken out")
    transaction_id: UUID
    sip_id: UUID | None
    created_at: datetime


class HoldingOut(BaseModel):
    id: UUID
    instrument_id: UUID
    instrument_code: str
    instrument_name: str
    kind: InstrumentKindLiteral
    risk_level: RiskLevelLiteral
    units_micro: int = Field(description="1 unit = 1,000,000 micro-units")
    price_minor: int
    price_date: date
    price_source: Literal["SIMULATED"] = Field(description=PRICE_SOURCE_DESCRIPTION)
    value_minor: int = Field(description="Units at today's simulated price, rounded down")
    cost_basis_minor: int = Field(description="Average cost of the units held")
    gain_minor: int = Field(description="Value minus cost basis (unrealised)")
    gain_bps: int | None = Field(description="Gain as basis points of the cost basis")


# ---------------------------------------------- insurance quote and application (ADR-0058 s. 8)
AgeBandLiteral = Literal["18-25", "26-35", "36-50", "51-59", "60+"]
ApplicationStatusLiteral = Literal["SUBMITTED", "APPROVED_SIMULATED", "DECLINED"]
MAX_COVER_MINOR = 100_00_00_000_00  # INR 100 crore: above every catalogue maximum


class InsuranceQuoteRequest(BaseModel):
    """The customer DECLARES an age band for this quote, with explicit consent; nothing is read
    from the profile (the ADR-0015 consent is for recommendations only). No premium from the
    client."""

    model_config = ConfigDict(extra="forbid")

    product_id: UUID
    cover_minor: int = Field(gt=0, le=MAX_COVER_MINOR)
    term_years: int = Field(gt=0, le=60)
    age_band: AgeBandLiteral
    age_band_consent: Literal[True] = Field(
        description="Must be true: I agree to my declared age band being used for this quote"
    )


class InsuranceQuoteOut(BaseModel):
    product_id: UUID
    product_name: str
    cover_minor: int
    term_years: int
    age_band: AgeBandLiteral
    annual_premium_minor: int | None = Field(
        description="Simulated annual premium (rule IQ-1, not an insurer's price); None if declined"
    )
    outcome: ApplicationStatusLiteral = Field(
        description="The status an application with these terms would get"
    )
    reason: str
    age_factor_bps: int = Field(description="The age band's factor (10000 = the 26-35 band)")
    term_factor_bps: int = Field(description="Term life term factor; 10000 for health plans")
    quote_version: str


class InsuranceApplicationOut(BaseModel):
    id: UUID
    product_id: UUID
    product_name: str
    cover_minor: int
    term_years: int
    age_band: AgeBandLiteral
    annual_premium_minor: int | None
    status: ApplicationStatusLiteral = Field(
        description="SUBMITTED = referred (no insurer decides it in PBIP); APPROVED_SIMULATED = "
        "meets the simulated rules, no policy is issued; DECLINED"
    )
    decision_reason: str
    quote_version: str
    created_at: datetime


# ------------------------------------------------- suitability by risk band (ADR-0058 s. 9)
class SuitabilityItemOut(BaseModel):
    product_id: UUID
    kind: Literal["WEALTH_PRODUCT", "INSTRUMENT", "INSURANCE"]
    risk_level: RiskLevelLiteral | None = Field(description="None for insurance (not risk-rated)")
    suitable: bool | None = Field(
        description="True: risk at or below my band; False: above it; None: no band or insurance"
    )
    reason: str


class SuitabilityOut(BaseModel):
    band: RiskLevelLiteral | None = Field(description="My RTQ-1 risk band; None if not assessed")
    questionnaire_version: str | None
    assessed_at: datetime | None
    items: list[SuitabilityItemOut]
