"""SIMULATED product uptake for the recommendation ranker experiment (TASK-054, issue #77).

**Nothing here is real customer behaviour.** PBIP has no recorded product take-ups. This module
invents a monthly customer panel and a documented, seeded uptake process so that a ranker can be
trained and compared with rules-v1 on the same data. Every coefficient is an assumption, listed with
its reasoning in ``docs/ml/model-cards/recommendation-propensity.md`` ("The simulation").

Independence from the rules (owner's instruction, 2026-11-04). The uptake labels are driven only by
the simulated customer's own behaviour and traits: income stability, savings rate, spending mix,
life-stage age band, existing holdings, clickstream interest (the TASK-112 ``product_clicked``
event) and noise. No rules-v1 score, eligibility rule or other recommendation-engine output feeds
them: this package does not import ``pbip_ml`` at all (an import-linter contract and a test hold
that). The ranker experiment applies the eligibility rules afterwards, when it builds candidates.

Determinism. Every customer draws from its own stream ``default_rng([seed, index, 0x5E2])``, so a
customer's history depends neither on the population size nor on the number of months. No wall
clock is read. Money is integer paise.

Order inside a month ``m``: (1) for ``m >= 1``, draw this month's uptakes from the state at the end
of month ``m - 1``; (2) apply their effects; (3) simulate month ``m`` (income, spending, clicks);
(4) emit the row for ``m``: the state at its end and the uptakes during it. A consumer that builds
features from rows ``<= t`` and reads the label from row ``t + 1`` therefore never sees the future.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Final

import numpy as np

from pbip_datagen.personas import PERSONAS, IncomePattern, Persona

UPTAKE_SIMULATION_VERSION: Final = "uptake-sim-2"
_STREAM_TAG: Final = 0x5E2  # separates these streams from the transaction generator's

PRODUCTS: Final[tuple[str, ...]] = (
    "savings_account",
    "fixed_deposit",
    "credit_line",
    "rewards_card",
    "index_sip",
    "family_cover",
)
AGE_BANDS: Final[tuple[str, ...]] = ("18-25", "26-35", "36-50", "51-59", "60+")
DISCRETIONARY_CATEGORIES: Final = frozenset({"dining", "shopping", "entertainment", "travel"})
FESTIVE_CALENDAR_MONTHS: Final = frozenset({10, 11})
START_CALENDAR_MONTH: Final = 1  # month 0 is January

# Life stage per persona: P(age band), in AGE_BANDS order.
AGE_BAND_WEIGHTS: Final[dict[str, tuple[float, ...]]] = {
    "salaried_professional": (0.15, 0.45, 0.35, 0.05, 0.0),
    "student": (1.0, 0.0, 0.0, 0.0, 0.0),
    "freelancer": (0.20, 0.45, 0.30, 0.05, 0.0),
    "business_owner": (0.0, 0.20, 0.50, 0.30, 0.0),
    "retiree": (0.0, 0.0, 0.0, 0.10, 0.90),
}
# Month-to-month income variation (coefficient of variation) and the chance of a thin month.
INCOME_CV: Final[dict[IncomePattern, float]] = {
    "salary": 0.03,
    "pension": 0.02,
    "allowance": 0.10,
    "freelance": 0.30,
    "business": 0.22,
}
THIN_MONTH_PROBABILITY: Final[dict[IncomePattern, float]] = {
    "salary": 0.0,
    "pension": 0.0,
    "allowance": 0.05,
    "freelance": 0.12,
    "business": 0.06,
}


@dataclass(frozen=True, slots=True)
class UptakeAssumptions:
    """Every number that shapes the labels. ``base_logit`` is the only part set by looking at
    data: it was calibrated (before any model was trained) so that monthly uptake lands at
    1-4% per product, enough positives for a test period; real response rates are lower."""

    base_logit: dict[str, float] = field(
        default_factory=lambda: {
            "savings_account": -4.6,
            "fixed_deposit": -6.6,
            "credit_line": -5.4,
            "rewards_card": -5.2,
            "index_sip": -6.2,
            "family_cover": -5.6,
        }
    )
    effect_scale: float = 1.0  # multiplies every behavioural and life-stage effect (variants)
    interest_effect: float = 0.5  # per unit of latent interest in the product
    clicks_effect: float = 0.5  # per log(1 + product_clicked events last month)
    responsiveness_sigma: float = 0.5  # per customer, every product
    monthly_shock_sigma: float = 0.3  # per customer, product and month
    background_rate: float = 0.0005  # uptake unrelated to anything, per product and month
    interest_persistence: float = 0.85  # AR(1) coefficient of the latent interest
    click_log_rate: float = -2.0  # log of the click rate at zero interest
    click_interest_effect: float = 0.9


@dataclass(frozen=True, slots=True)
class Behaviour:
    """What drives uptake at the end of a month: the customer's true state, before any bank
    derivation. The ranker never sees this object; it sees only the exported panel."""

    income_stability: float  # 1 - min(CV of the last 6 incomes / 0.4, 1)
    savings_rate: float  # (income - spending - EMI) / income, last 3 months
    discretionary_share: float  # dining, shopping, entertainment and travel, of spending
    discretionary_inr: float  # the same in rupees a month
    buffer_months: float  # balance / monthly outgoings
    age_band: str
    held: frozenset[str]


@dataclass(frozen=True, slots=True)
class PanelRow:
    """One customer-month as the bank's data would hold it, plus the uptakes during the month.
    ``held`` is ``KIND:amount_minor`` joined by ``;`` (kinds as in PBIP's held products)."""

    customer: int
    month: int
    persona: str
    age_band: str
    income_minor: int
    spend_minor: int  # living spending, excluding EMI
    discretionary_minor: int
    emi_minor: int
    balance_minor: int
    held: str
    clicks: tuple[int, ...]  # product_clicked events this month, in PRODUCTS order
    took: tuple[int, ...]  # 1 if taken up during this month, in PRODUCTS order


@dataclass(slots=True)
class _Customer:
    persona: Persona
    age_band: str
    base_income: float
    savings_target: float
    discretionary_share: float
    balance: float
    emi: float
    held: dict[str, int]  # kind -> amount in paise (FDs and loans summed per kind)
    responsiveness: float
    interest: np.ndarray
    taken_once: set[str] = field(default_factory=set)
    incomes: list[float] = field(default_factory=list)
    outgoings: list[float] = field(default_factory=list)
    clicks: tuple[int, ...] = (0, 0, 0, 0, 0, 0)


def _discretionary_share(persona: Persona, rng: np.random.Generator) -> float:
    """The persona's category weights, perturbed per customer (Dirichlet, concentration 40)."""
    names = sorted(persona.category_weights)
    draws = rng.dirichlet([40.0 * persona.category_weights[name] for name in names])
    return float(
        sum(w for name, w in zip(names, draws, strict=True) if name in DISCRETIONARY_CATEGORIES)
    )


def _initial_holdings(customer: _Customer, rng: np.random.Generator) -> None:
    persona = customer.persona
    if rng.random() < 0.6:
        customer.held["SAVINGS_ACCOUNT"] = 0
    else:
        customer.held["CURRENT_ACCOUNT"] = 0
    if rng.random() < persona.has_emi_probability:
        kind = str(rng.choice(["HOME_LOAN", "AUTO_LOAN", "PERSONAL_LOAN"], p=[0.40, 0.35, 0.25]))
        customer.emi = customer.base_income * rng.uniform(0.10, 0.35)
        customer.held[kind] = round(customer.emi * 36 * 100)
    if rng.random() < (0.30 if persona.name == "retiree" else 0.08):
        principal = customer.balance * 0.5
        customer.balance -= principal
        customer.held["FIXED_DEPOSIT"] = round(principal * 100)


def _new_customer(rng: np.random.Generator, assumptions: UptakeAssumptions) -> _Customer:
    persona = PERSONAS[int(rng.choice(len(PERSONAS), p=[p.share for p in PERSONAS]))]
    band = AGE_BANDS[int(rng.choice(len(AGE_BANDS), p=AGE_BAND_WEIGHTS[persona.name]))]
    low, high = persona.monthly_income_inr
    income = math.exp(rng.uniform(math.log(low), math.log(high)))
    savings_target = rng.uniform(*persona.savings_rate)
    customer = _Customer(
        persona=persona,
        age_band=band,
        base_income=income,
        savings_target=savings_target,
        discretionary_share=_discretionary_share(persona, rng),
        balance=income * rng.uniform(0.2, 1.0 + 20 * savings_target),
        emi=0.0,
        held={},
        responsiveness=assumptions.responsiveness_sigma * rng.normal(),
        interest=rng.normal(size=len(PRODUCTS)),
    )
    _initial_holdings(customer, rng)
    return customer


def _income(customer: _Customer, month: int, rng: np.random.Generator) -> float:
    pattern = customer.persona.income_pattern
    raise_factor = 1.07 ** (month // 12) if pattern == "salary" else 1.0
    income = customer.base_income * raise_factor * math.exp(INCOME_CV[pattern] * rng.normal())
    if rng.random() < THIN_MONTH_PROBABILITY[pattern]:
        income *= rng.uniform(0.1, 0.4)
    return income


def _evolve(
    customer: _Customer, month: int, rng: np.random.Generator, assumptions: UptakeAssumptions
) -> tuple[float, float]:
    """One month of income, spending and clicks. Returns (living spend, discretionary spend)."""
    income = _income(customer, month, rng)
    calendar_month = (START_CALENDAR_MONTH - 1 + month) % 12 + 1
    festive = calendar_month in FESTIVE_CALENDAR_MONTHS
    share = min(customer.discretionary_share * (1.25 if festive else 1.0), 0.9)
    spend = customer.base_income * (1.0 - customer.savings_target) - customer.emi
    spend = max(spend, 0.3 * customer.base_income) * math.exp(0.08 * rng.normal())
    spend *= 1.0 + (share - customer.discretionary_share)
    spend = min(spend, max(customer.balance + income - customer.emi, 0.0))  # no overdraft
    customer.balance = max(customer.balance + income - customer.emi - spend, 0.0)
    customer.incomes.append(income)
    customer.outgoings.append(spend + customer.emi)
    rho = assumptions.interest_persistence
    customer.interest = rho * customer.interest + math.sqrt(1 - rho * rho) * rng.normal(
        size=len(PRODUCTS)
    )
    rates = np.exp(
        assumptions.click_log_rate + assumptions.click_interest_effect * customer.interest
    )
    customer.clicks = tuple(int(c) for c in rng.poisson(rates))
    return spend, spend * share


def _behaviour(customer: _Customer, discretionary_inr: float, spend: float) -> Behaviour:
    incomes = customer.incomes[-6:]
    mean = sum(incomes) / len(incomes)
    cv = math.sqrt(sum((x - mean) ** 2 for x in incomes) / len(incomes)) / mean if mean else 1.0
    inflow, outflow = sum(customer.incomes[-3:]), sum(customer.outgoings[-3:])
    monthly_out = outflow / len(customer.outgoings[-3:])
    return Behaviour(
        income_stability=1.0 - min(cv / 0.4, 1.0),
        savings_rate=(inflow - outflow) / inflow if inflow > 0 else -1.0,
        discretionary_share=discretionary_inr / spend if spend > 0 else 0.0,
        discretionary_inr=discretionary_inr,
        buffer_months=customer.balance / monthly_out if monthly_out > 0 else 0.0,
        age_band=customer.age_band,
        held=frozenset(customer.held),
    )


def _clip(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


# Life-stage effects (logit), by product and age band. Unlisted bands: 0.
LIFE_STAGE: Final[dict[str, dict[str, float]]] = {
    "savings_account": {"18-25": 0.5},
    "fixed_deposit": {"18-25": -0.5, "51-59": 0.5, "60+": 0.9},
    "credit_line": {"26-35": 0.4, "60+": -0.8},
    "rewards_card": {"18-25": 0.2, "26-35": 0.4, "60+": -0.7},
    "index_sip": {"26-35": 0.6, "36-50": 0.3, "60+": -0.8},
    "family_cover": {"18-25": -0.8, "26-35": 0.4, "36-50": 0.7, "60+": -0.3},
}


def _savings_account(b: Behaviour) -> float:
    """Money left over and nowhere separate to put it; a steady income makes saving a habit."""
    already = -3.0 if "SAVINGS_ACCOUNT" in b.held else 0.0
    return 4.0 * _clip(b.savings_rate, 0.0, 0.4) + 0.8 * b.income_stability + already


def _fixed_deposit(b: Behaviour) -> float:
    """Idle cash, a predictable income and a frugal spending mix; repeat depositors roll over."""
    idle = 0.25 * min(b.buffer_months, 12.0)
    frugal = -1.2 * (b.discretionary_share - 0.3)
    repeat = 0.3 if "FIXED_DEPOSIT" in b.held else 0.0
    return idle + 1.5 * b.income_stability + 2.0 * _clip(b.savings_rate, 0.0, 0.4) + frugal + repeat


def _credit_line(b: Behaviour) -> float:
    """Demand comes from a squeeze: outgoings above income, a thin buffer, an irregular income."""
    squeeze = 6.0 * _clip(-b.savings_rate, 0.0, 0.3) + 1.0 * (b.buffer_months < 1.0)
    already = -1.0 if "PERSONAL_LOAN" in b.held else 0.0
    return squeeze + 1.0 * (1.0 - b.income_stability) + already


def _rewards_card(b: Behaviour) -> float:
    """Rewards pay off on high lifestyle spending, in rupees and as a share of the budget."""
    rupees = 1.2 * math.log(max(b.discretionary_inr, 1.0) / 10_000)
    return rupees + 2.0 * (b.discretionary_share - 0.3) + 0.6 * b.income_stability


def _index_sip(b: Behaviour) -> float:
    """A steady surplus, a cushion and a stable income to commit a monthly amount from."""
    surplus = 4.0 * _clip(b.savings_rate, 0.0, 0.5)
    return surplus + 0.10 * min(b.buffer_months, 12.0) + 1.0 * b.income_stability


def _family_cover(b: Behaviour) -> float:
    """A secured loan (home more than auto) and dependants to protect; stable earners plan."""
    loans = 1.4 * ("HOME_LOAN" in b.held) + 0.7 * ("AUTO_LOAN" in b.held)
    return loans + 0.8 * b.income_stability


BEHAVIOUR_EFFECTS: Final[dict[str, Callable[[Behaviour], float]]] = {
    "savings_account": _savings_account,
    "fixed_deposit": _fixed_deposit,
    "credit_line": _credit_line,
    "rewards_card": _rewards_card,
    "index_sip": _index_sip,
    "family_cover": _family_cover,
}
_ONE_OFF: Final = frozenset({"rewards_card", "index_sip", "family_cover"})  # no held kind in PBIP


def uptake_logit(
    product: str,
    behaviour: Behaviour,
    interest: float,
    clicks_last_month: int,
    assumptions: UptakeAssumptions,
) -> float:
    """The systematic part of the uptake logit (without responsiveness and the monthly shock)."""
    effect = BEHAVIOUR_EFFECTS[product](behaviour) + LIFE_STAGE[product].get(
        behaviour.age_band, 0.0
    )
    return (
        assumptions.base_logit[product]
        + assumptions.effect_scale * effect
        + assumptions.interest_effect * interest
        + assumptions.clicks_effect * math.log1p(clicks_last_month)
    )


def _draw_uptakes(
    customer: _Customer,
    behaviour: Behaviour,
    rng: np.random.Generator,
    assumptions: UptakeAssumptions,
) -> tuple[int, ...]:
    shocks = assumptions.monthly_shock_sigma * rng.normal(size=len(PRODUCTS))
    draws = rng.random(size=len(PRODUCTS))
    took: list[int] = []
    for i, product in enumerate(PRODUCTS):
        if product in customer.taken_once:
            took.append(0)
            continue
        logit = uptake_logit(
            product, behaviour, float(customer.interest[i]), customer.clicks[i], assumptions
        )
        logit += customer.responsiveness + float(shocks[i])
        p = assumptions.background_rate + (1 - assumptions.background_rate) / (1 + math.exp(-logit))
        took.append(int(draws[i] < p))
    return tuple(took)


def _apply(customer: _Customer, took: tuple[int, ...]) -> None:
    """Effects of an uptake. A card, SIP or cover has no held kind in PBIP, so it is taken once."""
    for i, product in enumerate(PRODUCTS):
        if not took[i]:
            continue
        customer.interest[i] -= 1.5  # interest falls once the need is met
        if product == "savings_account":
            customer.held["SAVINGS_ACCOUNT"] = 0
        elif product == "fixed_deposit":
            principal = customer.balance * 0.30
            customer.balance -= principal
            customer.held["FIXED_DEPOSIT"] = customer.held.get("FIXED_DEPOSIT", 0) + round(
                principal * 100
            )
        elif product == "credit_line":
            drawn = customer.base_income * 1.5
            customer.balance += drawn
            customer.emi += drawn / 24
            customer.held["PERSONAL_LOAN"] = customer.held.get("PERSONAL_LOAN", 0) + round(
                drawn * 100
            )
        else:
            customer.taken_once.add(product)


def _row(
    index: int, month: int, customer: _Customer, spend: float, disc: float, took: tuple[int, ...]
) -> PanelRow:
    return PanelRow(
        customer=index,
        month=month,
        persona=customer.persona.name,
        age_band=customer.age_band,
        income_minor=round(customer.incomes[-1] * 100),
        spend_minor=round(spend * 100),
        discretionary_minor=round(disc * 100),
        emi_minor=round(customer.emi * 100),
        balance_minor=round(customer.balance * 100),
        held=";".join(f"{kind}:{amount}" for kind, amount in sorted(customer.held.items())),
        clicks=customer.clicks,
        took=took,
    )


def simulate_customer(
    seed: int, index: int, months: int, assumptions: UptakeAssumptions | None = None
) -> list[PanelRow]:
    chosen = assumptions or UptakeAssumptions()
    rng = np.random.default_rng([seed, index, _STREAM_TAG])
    customer = _new_customer(rng, chosen)
    rows: list[PanelRow] = []
    behaviour: Behaviour | None = None
    for month in range(months):
        took = (0,) * len(PRODUCTS)
        if behaviour is not None:
            took = _draw_uptakes(customer, behaviour, rng, chosen)
            _apply(customer, took)
        spend, disc = _evolve(customer, month, rng, chosen)
        behaviour = _behaviour(customer, disc, spend)
        rows.append(_row(index, month, customer, spend, disc, took))
    return rows


def simulate_panel(
    seed: int, customers: int, months: int, assumptions: UptakeAssumptions | None = None
) -> list[PanelRow]:
    """Every customer-month, in (customer, month) order. Deterministic for a seed."""
    if customers < 1 or months < 2:
        raise ValueError("need at least 1 customer and 2 months")
    return [
        row
        for index in range(customers)
        for row in simulate_customer(seed, index, months, assumptions)
    ]


def perturbed(assumptions: UptakeAssumptions, scale: float, base_shift: float) -> UptakeAssumptions:
    """A held-out variant: every effect scaled and every base logit shifted."""
    return replace(
        assumptions,
        base_logit={key: value + base_shift for key, value in assumptions.base_logit.items()},
        effect_scale=assumptions.effect_scale * scale,
    )


def summary(rows: list[PanelRow]) -> dict[str, object]:
    """Uptake counts and monthly rates per product, for the export and the documentation."""
    labelled = [row for row in rows if row.month > 0]
    per_product = {
        product: {
            "uptakes": sum(row.took[i] for row in labelled),
            "monthly_rate": sum(row.took[i] for row in labelled) / max(len(labelled), 1),
            "click_rate": sum(row.clicks[i] for row in rows) / max(len(rows), 1),
        }
        for i, product in enumerate(PRODUCTS)
    }
    return {
        "simulation": UPTAKE_SIMULATION_VERSION,
        "data": "SIMULATED product uptake, not real customer behaviour",
        "customer_months": len(rows),
        "products": per_product,
    }
