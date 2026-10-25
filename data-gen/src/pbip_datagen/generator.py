"""Synthetic dataset v1: customers with personas, income calendars, EMIs, bills and spending.

Determinism contract: the same ``GeneratorConfig`` always yields byte-identical output.
* Every customer draws from its own stream ``default_rng([seed, index])``, so customer ``i`` is
  identical whether the run has 10 or 10,000 customers (prefix stability).
* Ids are UUIDv5 derived from the run id and position - never ``uuid4()`` - so reloading a run is
  idempotent and two runs never collide.
* No wall clock is read anywhere: dates come from ``config.start``.

Money is integer paise. v1 has no fraud labels (data-gen v2 injects F1-F6).
"""

import itertools
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

import numpy as np

from pbip_datagen.calendars import (
    APPRAISAL_MONTH,
    ELECTRICITY_SEASONALITY,
    FESTIVE_SPEND_MULTIPLIER,
    at_ist,
    clamp_day,
    days_in_month,
    last_business_day,
    month_starts,
    next_business_day,
)
from pbip_datagen.personas import PERSONAS, IncomePattern, Persona

GENERATOR_VERSION = "1.0.0"
_NAMESPACE = uuid.UUID("6f1c2b0e-8d4f-4c1a-9a57-3e2b7d9c4f10")
_MIN_AMOUNT_MINOR = 100  # one rupee

Direction = Literal["CREDIT", "DEBIT"]
EVENT_TYPES: tuple[str, ...] = (
    "SALARY",
    "PENSION",
    "ALLOWANCE",
    "FREELANCE_INCOME",
    "BUSINESS_INCOME",
    "EMI",
    "UTILITY",
    "DISCRETIONARY",
)
_INCOME_EVENT: dict[IncomePattern, str] = {
    "salary": "SALARY",
    "pension": "PENSION",
    "allowance": "ALLOWANCE",
    "freelance": "FREELANCE_INCOME",
    "business": "BUSINESS_INCOME",
}
_INCOME_SOURCE: dict[IncomePattern, str] = {
    "salary": "Employer Payroll",
    "pension": "Pension Disbursement",
    "allowance": "Family Transfer",
    "freelance": "Client Payment",
    "business": "Customer Receipts",
}
CITIES: tuple[tuple[str, float], ...] = (
    ("Mumbai", 0.18),
    ("Delhi", 0.17),
    ("Bengaluru", 0.15),
    ("Hyderabad", 0.10),
    ("Chennai", 0.10),
    ("Pune", 0.10),
    ("Kolkata", 0.08),
    ("Ahmedabad", 0.07),
    ("Jaipur", 0.05),
)
# Fictional merchant names (no real brands).
MERCHANTS: dict[str, tuple[str, ...]] = {
    "groceries": ("FreshCart", "Daily Basket", "Green Grocer Co", "Kirana Express"),
    "dining": ("Spice Route Cafe", "Tandoor House", "Bean and Brew", "Biryani Junction"),
    "transport": ("MetroRide", "CityCab", "FuelPoint", "RailPass"),
    "shopping": ("ShopSphere", "StyleKart", "Gadget Galaxy", "HomeNest"),
    "entertainment": ("CineMax", "StreamFlix", "PlayZone", "LiveTix"),
    "utilities": ("QuickRecharge", "WaterWorks Board", "GasConnect"),
    "travel": ("SkyHop Air", "StayEasy Hotels", "Yatra Lines", "TrailTrips"),
}
_PERSONA_SHARES = np.array([p.share for p in PERSONAS])
_CITY_SHARES = np.array([share for _, share in CITIES])


@dataclass(frozen=True, slots=True)
class GeneratorConfig:
    seed: int
    customers: int = 1000
    months: int = 12
    start: date = date(2025, 1, 1)

    def __post_init__(self) -> None:
        if self.seed < 0:
            raise ValueError("seed must be >= 0")
        if not 1 <= self.customers <= 1_000_000:
            raise ValueError("customers must be between 1 and 1,000,000")
        if not 1 <= self.months <= 120:
            raise ValueError("months must be between 1 and 120")
        if self.start.day != 1:
            raise ValueError("start must be the first day of a month")


@dataclass(frozen=True, slots=True)
class SyntheticCustomer:
    id: uuid.UUID
    index: int
    persona: str
    city: str
    monthly_income_minor: int
    savings_rate_bps: int
    has_emi: bool
    emi_minor: int
    emi_day: int | None
    joined_on: date
    churned_on: date | None


@dataclass(frozen=True, slots=True)
class SyntheticTransaction:
    id: uuid.UUID
    customer_id: uuid.UUID
    occurred_at: datetime
    direction: Direction
    amount_minor: int
    category: str
    event_type: str
    merchant: str


@dataclass(frozen=True, slots=True)
class SyntheticDataset:
    config: GeneratorConfig
    run_id: uuid.UUID
    customers: tuple[SyntheticCustomer, ...]
    transactions: tuple[SyntheticTransaction, ...]


@dataclass(frozen=True, slots=True)
class _Event:
    when: datetime
    direction: Direction
    amount_minor: int
    category: str
    event_type: str
    merchant: str


def run_id_for(config: GeneratorConfig) -> uuid.UUID:
    key = f"{GENERATOR_VERSION}:{config.seed}:{config.customers}:{config.months}:{config.start}"
    return uuid.uuid5(_NAMESPACE, key)


def _money(value: float) -> int:
    return max(_MIN_AMOUNT_MINOR, round(value))


def _churn_date(config: GeneratorConfig, persona: Persona, rng: np.random.Generator) -> date | None:
    for month in month_starts(config.start, config.months):
        if rng.random() < persona.monthly_churn_probability:
            return clamp_day(month, int(rng.integers(1, days_in_month(month) + 1)))
    return None


def make_customer(config: GeneratorConfig, run_id: uuid.UUID, index: int) -> SyntheticCustomer:
    rng = np.random.default_rng([config.seed, index])
    persona = PERSONAS[int(rng.choice(len(PERSONAS), p=_PERSONA_SHARES))]
    low, high = persona.monthly_income_inr
    income = int(rng.integers(low, high + 1)) * 100
    has_emi = bool(rng.random() < persona.has_emi_probability)
    return SyntheticCustomer(
        id=uuid.uuid5(run_id, f"customer:{index}"),
        index=index,
        persona=persona.name,
        city=CITIES[int(rng.choice(len(CITIES), p=_CITY_SHARES))][0],
        monthly_income_minor=income,
        savings_rate_bps=int(rng.uniform(*persona.savings_rate) * 10_000),
        has_emi=has_emi,
        emi_minor=int(income * rng.uniform(0.10, 0.35)) // 100 * 100 if has_emi else 0,
        emi_day=int(rng.choice([1, 5, 7, 10, 15])) if has_emi else None,
        joined_on=config.start - timedelta(days=int(rng.integers(0, 3 * 365))),
        churned_on=_churn_date(config, persona, rng),
    )


def _income_events(
    pattern: IncomePattern, month: date, income: int, rng: np.random.Generator
) -> list[_Event]:
    event, source = _INCOME_EVENT[pattern], _INCOME_SOURCE[pattern]
    if pattern == "salary":
        days, amounts = [last_business_day(month)], [float(income)]
    elif pattern == "pension":
        days, amounts = [next_business_day(month)], [float(income)]
    elif pattern == "allowance":
        days = [clamp_day(month, int(rng.integers(1, 6)))]
        amounts = [income * rng.uniform(0.9, 1.1)]
    else:  # freelance invoices / business receipts: lumpy and variable
        count = int(rng.integers(1, 5)) if pattern == "freelance" else int(rng.integers(2, 7))
        sigma = 0.35 if pattern == "freelance" else 0.40
        total = income * rng.lognormal(-(sigma**2) / 2, sigma)
        shares = rng.dirichlet(np.ones(count))
        days = [clamp_day(month, int(d)) for d in rng.integers(1, days_in_month(month) + 1, count)]
        amounts = [total * float(s) for s in shares]
    return [
        _Event(at_ist(d, int(rng.integers(540, 720))), "CREDIT", _money(a), "income", event, source)
        for d, a in zip(days, amounts, strict=True)
    ]


def _bill_events(
    customer: SyntheticCustomer, month: date, telecom_minor: int, rng: np.random.Generator
) -> list[_Event]:
    events: list[_Event] = []
    if customer.emi_day is not None:
        day = next_business_day(clamp_day(month, customer.emi_day))
        events.append(
            _Event(
                at_ist(day, int(rng.integers(360, 480))),
                "DEBIT",
                customer.emi_minor,
                "loan",
                "EMI",
                "HomeFin Loan EMI",
            )
        )
    base = min(max(customer.monthly_income_minor * 0.025, 50_000), 600_000)
    electricity = base * ELECTRICITY_SEASONALITY[month.month] * rng.lognormal(0, 0.08)
    bills = (
        ("State Power Distribution", electricity, 8, 15),
        ("TeleLink Mobile", float(telecom_minor), 3, 7),
    )
    for merchant, amount, first_day, last_day in bills:
        day = clamp_day(month, int(rng.integers(first_day, last_day)))
        when = at_ist(day, int(rng.integers(480, 1320)))
        events.append(_Event(when, "DEBIT", _money(amount), "utilities", "UTILITY", merchant))
    return events


def _discretionary_events(
    persona: Persona,
    month: date,
    rate: float,
    budget: float,
    rng: np.random.Generator,
) -> list[_Event]:
    count = int(rng.poisson(rate * FESTIVE_SPEND_MULTIPLIER.get(month.month, 1.0)))
    if count == 0:
        return []
    categories = list(persona.category_weights)
    weights = np.array([persona.category_weights[c] for c in categories])
    picks = rng.choice(len(categories), size=count, p=weights)
    sigma = 0.8
    amounts = (budget / count) * rng.lognormal(-(sigma**2) / 2, sigma, size=count)
    days = rng.integers(1, days_in_month(month) + 1, size=count)
    minutes = np.clip(rng.normal(19 * 60, 180, size=count), 7 * 60, 23 * 60 + 30)
    merchant_draws = rng.integers(0, 1 << 30, size=count)
    events = []
    columns = (picks.tolist(), amounts.tolist(), days.tolist(), minutes.tolist(), merchant_draws)
    rows = zip(*columns, strict=True)
    for pick, amount, day, minute, draw in rows:
        category = categories[pick]
        options = MERCHANTS[category]
        when = at_ist(month.replace(day=day), int(minute))
        merchant = options[int(draw) % len(options)]
        events.append(_Event(when, "DEBIT", _money(amount), category, "DISCRETIONARY", merchant))
    return events


def _monthly_events(
    customer: SyntheticCustomer,
    persona: Persona,
    month: date,
    income: int,
    profile: tuple[int, float],
    rng: np.random.Generator,
) -> list[_Event]:
    telecom_minor, spend_rate = profile
    events = _income_events(persona.income_pattern, month, income, rng)
    events += _bill_events(customer, month, telecom_minor, rng)
    fixed = sum(e.amount_minor for e in events if e.direction == "DEBIT")
    spendable = income * (1 - customer.savings_rate_bps / 10_000) - fixed
    budget = max(spendable, income * 0.10)
    events += _discretionary_events(persona, month, spend_rate, budget, rng)
    return events


def customer_transactions(
    config: GeneratorConfig, customer: SyntheticCustomer
) -> list[SyntheticTransaction]:
    """Every transaction for one customer over the configured months, oldest first."""
    persona = next(p for p in PERSONAS if p.name == customer.persona)
    rng = np.random.default_rng([config.seed, customer.index, 1])
    telecom_minor = int(rng.integers(399, 1000)) * 100
    profile = (telecom_minor, float(rng.uniform(*persona.discretionary_per_month)))
    income, events = customer.monthly_income_minor, []
    for offset, month in enumerate(month_starts(config.start, config.months)):
        if customer.churned_on is not None and month > customer.churned_on:
            break
        if month.month == APPRAISAL_MONTH and offset > 0:
            income = int(income * rng.uniform(1.04, 1.10)) // 100 * 100
        events += _monthly_events(customer, persona, month, income, profile, rng)
    if customer.churned_on is not None:
        events = [e for e in events if e.when.date() <= customer.churned_on]
    events.sort(key=lambda e: (e.when, e.event_type, e.amount_minor))
    # One UUIDv5 per customer, then a counter in the low 24 bits: deterministic, collision-free
    # within the run, and ~3x cheaper than hashing a UUIDv5 per row.
    base = uuid.uuid5(customer.id, "transactions").int & ~0xFFFFFF
    counter = itertools.count()
    return [
        SyntheticTransaction(
            id=uuid.UUID(int=base | next(counter)),
            customer_id=customer.id,
            occurred_at=e.when,
            direction=e.direction,
            amount_minor=e.amount_minor,
            category=e.category,
            event_type=e.event_type,
            merchant=e.merchant,
        )
        for e in events
    ]


def iter_customers(config: GeneratorConfig) -> Iterator[SyntheticCustomer]:
    run_id = run_id_for(config)
    for index in range(config.customers):
        yield make_customer(config, run_id, index)


def generate(config: GeneratorConfig) -> SyntheticDataset:
    customers = tuple(iter_customers(config))
    transactions = tuple(
        txn for customer in customers for txn in customer_transactions(config, customer)
    )
    return SyntheticDataset(config, run_id_for(config), customers, transactions)
