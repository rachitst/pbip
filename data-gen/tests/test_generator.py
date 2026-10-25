"""Generator v1 behaviour: determinism, personas and calendar events (no database)."""

from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date
from statistics import mean

import pytest
from pbip_datagen.calendars import IST, last_business_day, next_business_day
from pbip_datagen.generator import (
    EVENT_TYPES,
    GeneratorConfig,
    SyntheticDataset,
    SyntheticTransaction,
    customer_transactions,
    generate,
    run_id_for,
)
from pbip_datagen.personas import PERSONAS

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def dataset() -> SyntheticDataset:
    return generate(GeneratorConfig(seed=42, customers=400, months=12))


def _by_customer(dataset: SyntheticDataset) -> dict[object, list[SyntheticTransaction]]:
    grouped: dict[object, list[SyntheticTransaction]] = defaultdict(list)
    for txn in dataset.transactions:
        grouped[txn.customer_id].append(txn)
    return grouped


# ------------------------------------------------------------------ determinism


def test_same_seed_reproduces_the_dataset_exactly() -> None:
    config = GeneratorConfig(seed=7, customers=40, months=6)
    assert generate(config) == generate(config)


def test_different_seeds_give_different_data() -> None:
    a = generate(GeneratorConfig(seed=1, customers=40, months=3))
    b = generate(GeneratorConfig(seed=2, customers=40, months=3))
    assert [c.monthly_income_minor for c in a.customers] != [
        c.monthly_income_minor for c in b.customers
    ]
    assert a.run_id != b.run_id


def test_customers_are_prefix_stable_across_population_sizes() -> None:
    small = generate(GeneratorConfig(seed=9, customers=5, months=3))
    large = generate(GeneratorConfig(seed=9, customers=50, months=3))

    def profile(dataset: SyntheticDataset, index: int) -> tuple[object, ...]:
        c = dataset.customers[index]
        return (c.persona, c.city, c.monthly_income_minor, c.emi_minor, c.joined_on, c.churned_on)

    assert [profile(small, i) for i in range(5)] == [profile(large, i) for i in range(5)]


def test_ids_are_deterministic_and_unique(dataset: SyntheticDataset) -> None:
    config = dataset.config
    assert dataset.run_id == run_id_for(config)
    ids = [t.id for t in dataset.transactions]
    assert len(set(ids)) == len(ids)
    assert len({c.id for c in dataset.customers}) == len(dataset.customers)
    again = customer_transactions(config, dataset.customers[0])
    assert [t.id for t in again] == [
        t.id for t in dataset.transactions if t.customer_id == dataset.customers[0].id
    ]


@pytest.mark.parametrize(
    "kwargs",
    [{"seed": -1}, {"seed": 1, "customers": 0}, {"seed": 1, "months": 121},
     {"seed": 1, "start": date(2025, 1, 15)}],
)  # fmt: skip
def test_invalid_configs_are_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="must"):
        GeneratorConfig(**kwargs)  # type: ignore[arg-type]  # deliberately partial kwargs


# --------------------------------------------------------------------- personas


def test_all_required_personas_are_generated_in_proportion() -> None:
    customers = generate(GeneratorConfig(seed=3, customers=2000, months=1)).customers
    counts = Counter(c.persona for c in customers)
    for required in ("salaried_professional", "freelancer", "student", "retiree"):
        assert counts[required] > 0
    for persona in PERSONAS:
        assert abs(counts[persona.name] / len(customers) - persona.share) < 0.04


def test_customer_attributes_respect_persona_bounds(dataset: SyntheticDataset) -> None:
    personas = {p.name: p for p in PERSONAS}
    for c in dataset.customers:
        low, high = personas[c.persona].monthly_income_inr
        assert low * 100 <= c.monthly_income_minor <= high * 100
        assert 0 <= c.savings_rate_bps <= 10_000
        assert (c.emi_minor > 0 and c.emi_day is not None) == c.has_emi
        assert c.churned_on is None or c.churned_on >= c.joined_on


# ------------------------------------------------------------------ transactions


def test_transactions_are_well_formed(dataset: SyntheticDataset) -> None:
    for t in dataset.transactions:
        assert isinstance(t.amount_minor, int) and t.amount_minor > 0
        assert t.occurred_at.tzinfo is IST
        assert t.event_type in EVENT_TYPES
        assert t.direction == ("CREDIT" if t.category == "income" else "DEBIT")
    for txns in _by_customer(dataset).values():
        assert [t.occurred_at for t in txns] == sorted(t.occurred_at for t in txns)


def test_salaries_land_once_a_month_on_the_last_business_day(dataset: SyntheticDataset) -> None:
    grouped = _by_customer(dataset)
    salaried = [c for c in dataset.customers if c.persona == "salaried_professional"]
    assert salaried
    for c in salaried:
        credits = [t for t in grouped[c.id] if t.event_type == "SALARY"]
        months = {(t.occurred_at.year, t.occurred_at.month) for t in credits}
        assert len(months) == len(credits)  # never two salaries in a month
        for t in credits:
            day = t.occurred_at.date()
            assert day == last_business_day(day.replace(day=1))


def test_salary_is_revised_in_april(dataset: SyntheticDataset) -> None:
    grouped = _by_customer(dataset)
    c = next(
        c
        for c in dataset.customers
        if c.persona == "salaried_professional" and c.churned_on is None
    )
    amounts = {
        t.occurred_at.month: t.amount_minor for t in grouped[c.id] if t.event_type == "SALARY"
    }
    assert amounts[3] == c.monthly_income_minor
    assert amounts[4] > amounts[3] and amounts[12] == amounts[4]


def test_emis_are_fixed_monthly_debits_on_the_emi_day(dataset: SyntheticDataset) -> None:
    grouped = _by_customer(dataset)
    with_emi = [c for c in dataset.customers if c.has_emi and c.churned_on is None]
    assert with_emi
    for c in with_emi:
        emis = [t for t in grouped[c.id] if t.event_type == "EMI"]
        assert len(emis) == dataset.config.months
        assert {t.amount_minor for t in emis} == {c.emi_minor}
        assert c.emi_day is not None
        for t in emis:
            month = t.occurred_at.date().replace(day=1)
            assert t.occurred_at.date() == next_business_day(month.replace(day=c.emi_day))
    without = [c for c in dataset.customers if not c.has_emi]
    assert all(t.event_type != "EMI" for c in without for t in grouped[c.id])


def test_electricity_bills_spike_in_summer(dataset: SyntheticDataset) -> None:
    bills: dict[int, list[int]] = defaultdict(list)
    for t in dataset.transactions:
        if t.merchant == "State Power Distribution":
            bills[t.occurred_at.month].append(t.amount_minor)
    assert mean(bills[5]) > 1.4 * mean(bills[2])  # May vs February


def test_festive_months_lift_discretionary_spending(dataset: SyntheticDataset) -> None:
    per_month = Counter(
        t.occurred_at.month for t in dataset.transactions if t.event_type == "DISCRETIONARY"
    )
    assert per_month[11] > 1.2 * per_month[9]


def test_churned_customers_go_silent_after_churn(dataset: SyntheticDataset) -> None:
    grouped = _by_customer(dataset)
    churned = [c for c in dataset.customers if c.churned_on is not None]
    assert churned
    for c in churned:
        assert c.churned_on is not None
        assert all(t.occurred_at.date() <= c.churned_on for t in grouped[c.id])


def test_income_patterns_differ_by_persona(dataset: SyntheticDataset) -> None:
    events_by_persona: dict[str, set[str]] = defaultdict(set)
    persona_of = {c.id: c.persona for c in dataset.customers}
    for t in dataset.transactions:
        if t.direction == "CREDIT":
            events_by_persona[persona_of[t.customer_id]].add(t.event_type)
    assert events_by_persona["salaried_professional"] == {"SALARY"}
    assert events_by_persona["retiree"] == {"PENSION"}
    assert events_by_persona["student"] == {"ALLOWANCE"}
    assert events_by_persona["freelancer"] == {"FREELANCE_INCOME"}


def test_a_later_start_date_shifts_the_calendar() -> None:
    config = GeneratorConfig(seed=5, customers=3, months=2, start=date(2026, 3, 1))
    txns = generate(config).transactions
    assert {(t.occurred_at.year, t.occurred_at.month) for t in txns} <= {(2026, 3), (2026, 4)}
    assert generate(replace(config, start=date(2026, 5, 1))).run_id != generate(config).run_id
