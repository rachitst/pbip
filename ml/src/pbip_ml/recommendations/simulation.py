"""A SIMULATION of product uptake, to train and evaluate the propensity model (TASK-054, ADR-0049).

**Nothing here is real customer behaviour.** PBIP has no recorded product take-ups
(``docs/ml/recommendation-propensity-decision.md``). This module invents a population and a
documented, seeded uptake process so that a propensity model can be trained, evaluated against
rules-v1 on the same data, and labelled honestly. Every number below is an assumption chosen to be
plausible, not measured; ``docs/ml/model-cards/recommendation-propensity.md`` lists them all with
the reasoning. A result on this data says how well a ranker recovers these assumptions.

Process, per simulated customer (each with its own RNG stream ``[seed, index]``, so a customer's
history does not depend on the population size or on how many months are simulated):

1. A persona (salaried, gig worker, affluent, retiree, student) sets income, spending, lifestyle
   share, the starting cash buffer and the chance of holding a loan.
2. Each month income and spending move with noise (festive months spend more, salaried customers
   get a yearly raise), the balance carries forward, and the context the engine sees is derived
   from the last three months: the same ``CustomerRecommendationContext`` the API builds.
3. For each product the rules allow that month (``evaluate_eligibility`` and
   ``held_product_rule``: the bank only sells what its rules allow), the customer takes it up
   next month with probability ``sigmoid(logit)``, where the logit is the product's assumption
   below plus three **unobserved** traits (responsiveness, risk appetite, credit appetite) that no
   model input carries, so a perfect ranker is impossible. A further share of uptakes is pure
   noise, unrelated to anything.
4. An uptake changes the state: a savings account or a credit line becomes held, a fixed deposit
   moves 30% of the balance into a held deposit. A card, SIP or cover is not a held kind in PBIP
   (ADR-0030), so after one the customer leaves that product's rows (it is never taken twice).

Scenario ``RULES_FOLLOW`` replaces the product assumptions with rules-v1's own relevance formula,
as a sanity check: on it rules-v1 is the right answer, so a model must not beat it by much.

Offline/training code only; never imported on the request path (import-linter).
"""

import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date
from enum import StrEnum
from uuid import UUID, uuid5

from pbip_ml.recommendations.contract import (
    BankingProduct,
    CustomerRecommendationContext,
    HeldProduct,
    HeldProductKind,
)
from pbip_ml.recommendations.engine import (
    CREDIT_LINE_ID,
    FAMILY_COVER_ID,
    FIXED_DEPOSIT_ID,
    INDEX_SIP_ID,
    PRODUCT_CATALOGUE,
    REWARDS_CARD_ID,
    SAVINGS_ACCOUNT_ID,
    evaluate_eligibility,
    held_product_rule,
    relevance_score,
)

SIMULATION_VERSION = "uptake-sim-1"
START = date(2025, 1, 1)  # month 0 of the simulated calendar
HISTORY_MONTHS = 3  # the context averages the last three months, like the analytics module
FESTIVE_MONTHS = frozenset({10, 11})  # Diwali season: discretionary spend rises
_NAMESPACE = UUID("6f1d7c3e-6a51-4c55-9a8e-2f3b0c7d9e11")
_LOAN_KINDS = (HeldProductKind.HOME_LOAN, HeldProductKind.AUTO_LOAN, HeldProductKind.PERSONAL_LOAN)


class Scenario(StrEnum):
    DOCUMENTED = "DOCUMENTED"  # the assumptions in UptakeAssumptions (train and test)
    RULES_FOLLOW = "RULES_FOLLOW"  # uptake follows rules-v1's relevance score (sanity check)


@dataclass(frozen=True, slots=True)
class Persona:
    name: str
    share: float
    income_median_inr: float
    income_sigma: float  # between-customer spread (lognormal)
    income_cv: float  # month-to-month variation
    spend_fraction: tuple[float, float]  # spending / income, uniform range
    discretionary: tuple[float, float]
    start_buffer_months: tuple[float, float]
    loan_probability: float
    savings_account_probability: float
    deposit_probability: float


PERSONAS: tuple[Persona, ...] = (
    Persona(
        "SALARIED",
        0.45,
        55_000,
        0.5,
        0.05,
        (0.60, 0.95),
        (0.15, 0.40),
        (0.5, 4.0),
        0.40,
        0.80,
        0.08,
    ),
    Persona(
        "GIG", 0.20, 30_000, 0.6, 0.35, (0.80, 1.10), (0.20, 0.45), (0.0, 1.5), 0.25, 0.55, 0.02
    ),
    Persona(
        "AFFLUENT",
        0.12,
        250_000,
        0.4,
        0.15,
        (0.40, 0.70),
        (0.25, 0.50),
        (4.0, 18.0),
        0.45,
        0.90,
        0.30,
    ),
    Persona(
        "RETIREE",
        0.13,
        35_000,
        0.4,
        0.03,
        (0.60, 0.90),
        (0.05, 0.20),
        (3.0, 15.0),
        0.05,
        0.85,
        0.35,
    ),
    Persona(
        "STUDENT", 0.10, 12_000, 0.4, 0.20, (0.85, 1.05), (0.30, 0.55), (0.0, 1.0), 0.02, 0.50, 0.00
    ),
)


@dataclass(frozen=True, slots=True)
class UptakeAssumptions:
    """Monthly uptake logits per product. Invented, documented, deliberately not rules-v1's
    weights: each reflects a common-sense driver (stated in the model card) at a made-up size."""

    base_logit: dict[UUID, float] = field(
        default_factory=lambda: {
            SAVINGS_ACCOUNT_ID: -5.0,
            FIXED_DEPOSIT_ID: -6.3,
            CREDIT_LINE_ID: -5.0,
            REWARDS_CARD_ID: -4.8,
            INDEX_SIP_ID: -6.4,
            FAMILY_COVER_ID: -5.0,
        }
    )
    effect_scale: float = 1.0  # multiplies every behavioural effect (robustness variants)
    noise_uptake_rate: float = 0.0005  # monthly uptake unrelated to anything, per eligible product
    responsiveness_sigma: float = 0.5  # unobserved: how readily this customer takes up anything
    risk_appetite_effect: float = 0.9  # unobserved: SIP up, deposit down
    credit_appetite_effect: float = 0.7  # unobserved: card and credit line up
    rules_follow_gain: float = 2.0  # RULES_FOLLOW: logit = base + gain * logit(relevance)


@dataclass(slots=True)
class _Customer:
    persona: Persona
    base_income: float  # INR per month
    base_spend_fraction: float
    discretionary: float
    balance: float  # INR
    emi: float  # INR per month
    held: list[HeldProduct]
    segment_override: str | None  # a new customer the weekly segmentation has not reached
    responsiveness: float
    risk_appetite: float
    credit_appetite: float
    taken: set[UUID] = field(default_factory=set)
    incomes: list[float] = field(default_factory=list)
    spends: list[float] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One customer-month: what the engine would see at the end of ``month``, the products the
    rules allowed, and which of them the customer took up during the following month."""

    customer: int
    month: int
    context: CustomerRecommendationContext
    offerable: tuple[UUID, ...]
    taken_next_month: frozenset[UUID]


def _deposit(principal_inr: float) -> HeldProduct:
    return HeldProduct(kind=HeldProductKind.FIXED_DEPOSIT, amount_minor=round(principal_inr * 100))


def _uniform(rng: random.Random, bounds: tuple[float, float]) -> float:
    return rng.uniform(bounds[0], bounds[1])


def _new_customer(rng: random.Random, assumptions: UptakeAssumptions) -> _Customer:
    persona = rng.choices(PERSONAS, weights=[p.share for p in PERSONAS])[0]
    income = persona.income_median_inr * math.exp(persona.income_sigma * rng.gauss(0.0, 1.0))
    spend_fraction = _uniform(rng, persona.spend_fraction)
    held = [
        HeldProduct(kind=HeldProductKind.SAVINGS_ACCOUNT)
        if rng.random() < persona.savings_account_probability
        else HeldProduct(kind=HeldProductKind.CURRENT_ACCOUNT)
    ]
    emi = 0.0
    if rng.random() < persona.loan_probability:
        kind = rng.choices(_LOAN_KINDS, weights=(0.40, 0.35, 0.25))[0]
        emi = income * _uniform(rng, (0.10, 0.40))
        held.append(HeldProduct(kind=kind, amount_minor=round(emi * 36 * 100)))
    balance = income * spend_fraction * _uniform(rng, persona.start_buffer_months)
    if rng.random() < persona.deposit_probability:
        principal = balance * 0.5
        balance -= principal
        held.append(
            HeldProduct(kind=HeldProductKind.FIXED_DEPOSIT, amount_minor=round(principal * 100))
        )
    return _Customer(
        persona=persona,
        base_income=income,
        base_spend_fraction=spend_fraction,
        discretionary=_uniform(rng, persona.discretionary),
        balance=balance,
        emi=emi,
        held=held,
        segment_override="UNSEGMENTED" if rng.random() < 0.15 else None,
        responsiveness=assumptions.responsiveness_sigma * rng.gauss(0.0, 1.0),
        risk_appetite=rng.gauss(0.0, 1.0),
        credit_appetite=rng.gauss(0.0, 1.0),
    )


def _evolve(customer: _Customer, month: int, rng: random.Random) -> None:
    """One month of income and spending. The balance never goes below zero (no overdraft)."""
    persona = customer.persona
    calendar_month = (START.month - 1 + month) % 12 + 1
    raise_factor = 1.06 ** (month // 12) if persona.name == "SALARIED" else 1.0
    income = customer.base_income * raise_factor * math.exp(persona.income_cv * rng.gauss(0.0, 1.0))
    festive = 1.0 + (0.15 * customer.discretionary * 4 if calendar_month in FESTIVE_MONTHS else 0.0)
    spend = customer.base_income * customer.base_spend_fraction * festive
    spend *= math.exp(0.08 * rng.gauss(0.0, 1.0))
    available = max(customer.balance + income - customer.emi, 0.0)
    spend = min(spend, available)  # nobody spends money they do not have
    customer.balance = available - spend
    customer.incomes.append(income)
    customer.spends.append(spend + customer.emi)


def _health(savings: float, buffer: float, emi_ratio: float, income_cv: float) -> int:
    """A simplified 0-100 score with the analytics components (savings rate, buffer, debt,
    income stability), 25 points each. An approximation, not the API's exact formula."""
    points = 25 * min(max(savings, 0.0) / 0.20, 1.0)
    points += 25 * min(buffer / 6.0, 1.0)
    points += 25 * (1.0 - min(max(emi_ratio - 0.20, 0.0) / 0.40, 1.0))
    points += 25 * (1.0 - min(income_cv / 0.40, 1.0))
    return round(points)


def _segment(customer: _Customer, inflow: float, savings: float) -> str:
    """Stand-in for the weekly k-means labels: the persona-level traits each label describes."""
    if customer.segment_override:
        return customer.segment_override
    if inflow >= 150_000 or customer.balance >= 1_000_000:
        return "HIGH_NET_WORTH"
    if customer.discretionary >= 0.35:
        return "DISCRETIONARY_SPENDER"
    if customer.persona.name in {"GIG", "STUDENT"}:
        return "FREQUENT_TRANSACTOR"
    if savings >= 0.15:
        return "BUDGET_CONSCIOUS"
    return "INACTIVE_SAVER"


def _context(customer: _Customer, customer_id: UUID) -> CustomerRecommendationContext:
    incomes = customer.incomes[-HISTORY_MONTHS:]
    spends = customer.spends[-HISTORY_MONTHS:]
    inflow, spend = sum(incomes) / len(incomes), sum(spends) / len(spends)
    savings = (inflow - spend) / inflow if inflow > 0 else 0.0
    buffer = customer.balance / spend if spend > 0 else 0.0
    emi_ratio = customer.emi / inflow if inflow > 0 else 0.0
    mean = sum(incomes) / len(incomes)
    cv = math.sqrt(sum((x - mean) ** 2 for x in incomes) / len(incomes)) / mean if mean else 0.0
    return CustomerRecommendationContext(
        customer_id=customer_id,
        segment=_segment(customer, inflow, savings),
        health_score=_health(savings, buffer, emi_ratio, cv),
        monthly_inflow_minor=round(inflow * 100),
        monthly_spend_minor=round(spend * 100),
        savings_ratio=round(savings, 4),
        discretionary_ratio=round(customer.discretionary, 4),
        buffer_months=round(buffer, 2),
        emi_ratio=round(emi_ratio, 4),
        total_balance_minor=round(customer.balance * 100),
        held_products=tuple(customer.held),
    )


def _offerable(customer: _Customer, ctx: CustomerRecommendationContext) -> tuple[UUID, ...]:
    """The products the rules allow now and that the customer has not already taken once."""
    return tuple(
        product.product_id
        for product in PRODUCT_CATALOGUE
        if product.product_id not in customer.taken
        and evaluate_eligibility(product, ctx)
        and held_product_rule(product, ctx)[0]
    )


def _held(ctx: CustomerRecommendationContext, kind: HeldProductKind) -> float:
    return 1.0 if any(item.kind == kind for item in ctx.held_products) else 0.0


def _savings_account(ctx: CustomerRecommendationContext) -> float:
    """Money left over each month and nowhere separate to put it."""
    surplus = min(max(ctx.savings_ratio, 0.0), 0.4)
    return 6.0 * surplus + 0.5 * math.log1p(ctx.total_balance_minor / 100 / 100_000)


def _fixed_deposit(ctx: CustomerRecommendationContext) -> float:
    """Idle cash: more buffer, more likely, but very large buffers are invested elsewhere; a
    frugal lifestyle helps; a customer who already holds a deposit is less likely to add one."""
    buffer = min(ctx.buffer_months, 24.0)
    idle = 0.35 * min(buffer, 12.0) - 0.15 * max(buffer - 12.0, 0.0)
    frugal = 1.0 * (ctx.discretionary_ratio < 0.15)
    return idle + frugal - 0.6 * _held(ctx, HeldProductKind.FIXED_DEPOSIT)


def _credit_line(ctx: CustomerRecommendationContext) -> float:
    """Borrowing demand comes from a squeeze (spending above income, little buffer), not from
    comfort; a higher income helps a little."""
    squeeze = 8.0 * min(max(-ctx.savings_ratio, 0.0), 0.3) + 1.2 * (ctx.buffer_months < 1.0)
    return squeeze + 0.3 * math.log(max(ctx.monthly_inflow_minor / 100, 1.0) / 25_000)


def _rewards_card(ctx: CustomerRecommendationContext) -> float:
    """Rewards are worth it on high lifestyle spend in rupees (not as a share)."""
    lifestyle_inr = ctx.monthly_spend_minor * ctx.discretionary_ratio / 100
    return 1.3 * math.log(max(lifestyle_inr, 1.0) / 10_000) + 0.6 * (
        ctx.segment == "FREQUENT_TRANSACTOR"
    )


def _index_sip(ctx: CustomerRecommendationContext) -> float:
    """A steady surplus and a cushion, and a higher income."""
    surplus = 5.0 * min(max(ctx.savings_ratio, 0.0), 0.5)
    cushion = 0.12 * min(ctx.buffer_months, 12.0)
    return surplus + cushion + 0.4 * math.log(max(ctx.monthly_inflow_minor / 100, 1.0) / 50_000)


def _family_cover(ctx: CustomerRecommendationContext) -> float:
    """A secured loan to protect (home more than auto), and commitments relative to income."""
    loans = 1.6 * _held(ctx, HeldProductKind.HOME_LOAN) + 0.9 * _held(
        ctx, HeldProductKind.AUTO_LOAN
    )
    income = 0.3 * math.log(max(ctx.monthly_inflow_minor / 100, 1.0) / 30_000)
    return loans + 2.0 * min(ctx.emi_ratio, 0.6) + income


# The observable part of the documented assumptions (model card, "Uptake assumptions").
_BEHAVIOUR: dict[UUID, Callable[[CustomerRecommendationContext], float]] = {
    SAVINGS_ACCOUNT_ID: _savings_account,
    FIXED_DEPOSIT_ID: _fixed_deposit,
    CREDIT_LINE_ID: _credit_line,
    REWARDS_CARD_ID: _rewards_card,
    INDEX_SIP_ID: _index_sip,
    FAMILY_COVER_ID: _family_cover,
}


def _latent(product_id: UUID, customer: _Customer, assumptions: UptakeAssumptions) -> float:
    """The unobserved traits. No model input carries them."""
    if product_id == INDEX_SIP_ID:
        return assumptions.risk_appetite_effect * customer.risk_appetite
    if product_id == FIXED_DEPOSIT_ID:
        return -0.5 * assumptions.risk_appetite_effect * customer.risk_appetite
    if product_id in (CREDIT_LINE_ID, REWARDS_CARD_ID):
        return assumptions.credit_appetite_effect * customer.credit_appetite
    return 0.0


def _uptake_probability(
    product: BankingProduct,
    ctx: CustomerRecommendationContext,
    customer: _Customer,
    assumptions: UptakeAssumptions,
    scenario: Scenario,
) -> float:
    base = assumptions.base_logit[product.product_id]
    if scenario is Scenario.RULES_FOLLOW:
        relevance = min(max(relevance_score(product, ctx), 1e-4), 1 - 1e-4)
        signal = assumptions.rules_follow_gain * math.log(relevance / (1 - relevance))
    else:
        signal = assumptions.effect_scale * _BEHAVIOUR[product.product_id](ctx)
    logit = (
        base + signal + customer.responsiveness + _latent(product.product_id, customer, assumptions)
    )
    behavioural = 1.0 / (1.0 + math.exp(-logit))
    return assumptions.noise_uptake_rate + (1 - assumptions.noise_uptake_rate) * behavioural


def _apply_uptake(customer: _Customer, taken: frozenset[UUID]) -> None:
    for product_id in taken:
        if product_id == SAVINGS_ACCOUNT_ID:
            customer.held.append(HeldProduct(kind=HeldProductKind.SAVINGS_ACCOUNT))
        elif product_id == FIXED_DEPOSIT_ID:
            principal = customer.balance * 0.30
            customer.balance -= principal
            customer.held.append(
                HeldProduct(kind=HeldProductKind.FIXED_DEPOSIT, amount_minor=round(principal * 100))
            )
        elif product_id == CREDIT_LINE_ID:
            drawn = customer.base_income * 1.5
            customer.emi += drawn / 24
            customer.balance += drawn
            customer.held.append(
                HeldProduct(kind=HeldProductKind.PERSONAL_LOAN, amount_minor=round(drawn * 100))
            )
        else:  # card, SIP, cover: no held kind in PBIP; never offered to this customer again
            customer.taken.add(product_id)


_BY_ID = {product.product_id: product for product in PRODUCT_CATALOGUE}


def _simulate_customer(
    seed: int, index: int, months: int, assumptions: UptakeAssumptions, scenario: Scenario
) -> list[Snapshot]:
    rng = random.Random(f"{SIMULATION_VERSION}:{seed}:{index}")  # noqa: S311 - simulation, not security
    customer = _new_customer(rng, assumptions)
    customer_id = uuid5(_NAMESPACE, f"{seed}:{index}")
    snapshots: list[Snapshot] = []
    for month in range(months):
        _evolve(customer, month, rng)
        if month < HISTORY_MONTHS - 1:
            continue
        ctx = _context(customer, customer_id)
        offerable = _offerable(customer, ctx)
        if month == months - 1:
            break  # the following month is not simulated, so this month has no label
        draws = [rng.random() for _ in offerable]
        taken = frozenset(
            pid
            for pid, u in zip(offerable, draws, strict=True)
            if u < _uptake_probability(_BY_ID[pid], ctx, customer, assumptions, scenario)
        )
        snapshots.append(Snapshot(index, month, ctx, offerable, taken))
        _apply_uptake(customer, taken)
    return snapshots


def simulate(
    seed: int,
    customers: int,
    months: int,
    scenario: Scenario = Scenario.DOCUMENTED,
    assumptions: UptakeAssumptions | None = None,
) -> list[Snapshot]:
    """Every labelled customer-month, in (customer, month) order. Deterministic for a seed."""
    chosen = assumptions or UptakeAssumptions()
    return [
        snapshot
        for index in range(customers)
        for snapshot in _simulate_customer(seed, index, months, chosen, scenario)
    ]


def perturbed(assumptions: UptakeAssumptions, scale: float, base_shift: float) -> UptakeAssumptions:
    """A held-out variant: every effect scaled and every base rate shifted (robustness check)."""
    return replace(
        assumptions,
        effect_scale=assumptions.effect_scale * scale,
        base_logit={key: value + base_shift for key, value in assumptions.base_logit.items()},
    )
