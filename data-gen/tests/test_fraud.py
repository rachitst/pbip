"""TASK-023: labelled fraud scenarios F1-F6 are injected deterministically and cleanly."""

import json
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import pytest
from pbip_datagen.__main__ import main
from pbip_datagen.fraud import (
    SCENARIOS,
    FraudConfig,
    LabelledTransfers,
    Transfer,
    customer_transfers,
    generate_labelled,
)
from pbip_datagen.generator import GeneratorConfig, iter_customers

CONFIG = GeneratorConfig(seed=7, customers=3000, months=6)


@pytest.fixture(scope="module")
def data() -> LabelledTransfers:
    return generate_labelled(CONFIG)


def _incidents(data: LabelledTransfers, scenario: str) -> list[list[tuple[Transfer, str]]]:
    by_id = {t.id: t for t in data.transfers}
    grouped: dict[object, list[tuple[Transfer, str]]] = defaultdict(list)
    for label in data.labels:
        if label.scenario == scenario:
            grouped[label.incident_id].append((by_id[label.transaction_id], label.role))
    return list(grouped.values())


def test_same_seed_same_output_and_another_seed_differs() -> None:
    small = GeneratorConfig(seed=7, customers=300, months=3)
    assert generate_labelled(small) == generate_labelled(small)
    assert (
        generate_labelled(small).labels
        != generate_labelled(GeneratorConfig(seed=8, customers=300, months=3)).labels
    )


def test_a_customer_is_identical_whatever_the_run_size() -> None:
    fraud = FraudConfig()
    few = list(iter_customers(GeneratorConfig(seed=7, customers=10, months=3)))
    many = list(iter_customers(GeneratorConfig(seed=7, customers=60, months=3)))
    for a, b in zip(few, many, strict=False):
        cfg_a = GeneratorConfig(seed=7, customers=10, months=3)
        cfg_b = GeneratorConfig(seed=7, customers=60, months=3)
        rows_a, labels_a = customer_transfers(cfg_a, fraud, a)
        rows_b, labels_b = customer_transfers(cfg_b, fraud, b)
        # ids derive from the run (which includes its size); the random draws do not
        assert [(t.occurred_at, t.amount_minor, t.direction) for t in rows_a] == [
            (t.occurred_at, t.amount_minor, t.direction) for t in rows_b
        ]
        assert [lb.scenario for lb in labels_a] == [lb.scenario for lb in labels_b]


def test_every_scenario_is_present_and_the_summary_counts_the_labels(
    data: LabelledTransfers,
) -> None:
    summary = data.summary()
    scenarios = summary["scenarios"]
    assert isinstance(scenarios, dict)
    for scenario in SCENARIOS:
        incidents = _incidents(data, scenario)
        assert len(incidents) == scenarios[scenario]["incidents"] > 0
    assert summary["fraud_transactions"] == sum(lb.fraudulent for lb in data.labels)
    ids = {t.id for t in data.transfers}
    assert all(lb.transaction_id in ids for lb in data.labels)  # every label points at a row
    assert len({lb.transaction_id for lb in data.labels}) == len(data.labels)


def test_rates_are_honoured(data: LabelledTransfers) -> None:
    rates = FraudConfig().rates
    for scenario in SCENARIOS:
        expected = rates[scenario] * CONFIG.customers
        assert 0.5 * expected <= len(_incidents(data, scenario)) <= 1.6 * expected, scenario


def test_f1_new_payee_and_large_transfer(data: LabelledTransfers) -> None:
    for ((row, _),) in _incidents(data, "F1"):
        assert row.payee_added_at is not None
        assert row.occurred_at - row.payee_added_at < timedelta(hours=1)
        assert row.amount_minor >= 100_000  # at least INR 1,000 (1.5x+ monthly income)


def test_f2_burst_within_thirty_minutes(data: LabelledTransfers) -> None:
    for incident in _incidents(data, "F2"):
        times = sorted(row.occurred_at for row, _ in incident)
        assert len(times) >= 6
        assert times[-1] - times[0] <= timedelta(minutes=30)


def test_f3_failed_logins_then_new_device(data: LabelledTransfers) -> None:
    for ((row, role),) in _incidents(data, "F3"):
        assert role == "takeover" and row.failed_logins_24h >= 3 and row.new_device


def test_f4_home_then_abroad_within_three_hours(data: LabelledTransfers) -> None:
    for incident in _incidents(data, "F4"):
        legs = {role: row for row, role in incident}
        assert legs["home"].ip_country == "IN" and legs["abroad"].ip_country != "IN"
        assert (
            timedelta(0)
            < legs["abroad"].occurred_at - legs["home"].occurred_at
            <= timedelta(hours=3)
        )
    home_labels = [lb for lb in data.labels if lb.scenario == "F4" and lb.role == "home"]
    assert home_labels and not any(lb.fraudulent for lb in home_labels)  # context, not fraud


def test_f5_amount_far_above_the_customers_baseline(data: LabelledTransfers) -> None:
    by_customer: dict[object, list[int]] = defaultdict(list)
    labelled = {lb.transaction_id for lb in data.labels}
    for t in data.transfers:
        if t.direction == "DEBIT" and t.id not in labelled:
            by_customer[t.customer_id].append(t.amount_minor)
    for ((row, _),) in _incidents(data, "F5"):
        baseline = sorted(by_customer[row.customer_id])
        median = baseline[len(baseline) // 2]
        assert row.amount_minor >= 5 * median


def test_f6_inflow_from_several_senders_passed_on_within_six_hours(
    data: LabelledTransfers,
) -> None:
    for incident in _incidents(data, "F6"):
        inbound = [row for row, role in incident if role == "inbound"]
        (outbound,) = [row for row, role in incident if role == "outbound"]
        assert len({row.counterparty_id for row in inbound}) >= 3
        inflow = sum(row.amount_minor for row in inbound)
        assert 0.89 * inflow <= outbound.amount_minor <= 0.99 * inflow
        assert outbound.occurred_at - max(r.occurred_at for r in inbound) <= timedelta(hours=6)


def test_ordinary_transfers_never_match_a_scenario_signature(data: LabelledTransfers) -> None:
    """Normal customers do first-time things (#251), but never a scenario's FULL signature."""
    labelled = {lb.transaction_id for lb in data.labels}
    income = {c.id: c.monthly_income_minor for c in iter_customers(CONFIG)}
    for t in data.transfers:
        if t.id in labelled:
            continue
        assert t.direction == "DEBIT" and t.payee_added_at is not None and t.ip_country == "IN"
        new_payee = t.occurred_at - t.payee_added_at < timedelta(days=30)
        if new_payee:  # never F1's 1.5x monthly income
            assert t.amount_minor < 1.5 * income[t.customer_id]
        if t.new_device:  # never F3's failed logins or new payee
            assert t.failed_logins_24h == 0 and not new_payee
        if t.failed_logins_24h:
            assert not t.new_device


def test_legitimate_first_time_behaviour_is_present(data: LabelledTransfers) -> None:
    """#251: legitimate transfers to new payees, new phones and failed logins all exist."""
    labelled = {lb.transaction_id for lb in data.labels}
    normal = [t for t in data.transfers if t.id not in labelled]
    assert any(
        t.occurred_at - t.payee_added_at < timedelta(hours=1) for t in normal if t.payee_added_at
    )
    within_3_days = sum(
        1
        for t in normal
        if t.payee_added_at and t.occurred_at - t.payee_added_at < timedelta(days=3)
    )
    assert within_3_days > 500
    assert (
        sum(t.new_device for t in normal) > 100
        and sum(t.failed_logins_24h > 0 for t in normal) > 100
    )


def test_fraud_config_is_validated() -> None:
    with pytest.raises(ValueError, match=r"F1-F6"):
        FraudConfig(rates={"F1": 0.1})  # type: ignore[dict-item]  # deliberately incomplete
    with pytest.raises(ValueError, match=r"at most 0\.5"):
        FraudConfig(rates=dict.fromkeys(SCENARIOS, 0.1))  # type: ignore[arg-type]  # Literal keys


def test_export_writes_labels_and_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            ["export", "--seed", "3", "--customers", "400", "--months", "3", "--out", str(tmp_path)]
        )
        == 0
    )
    for name in ("transfers.csv", "fraud_labels.csv", "fraud_summary.json"):
        assert (tmp_path / name).exists()
    summary = json.loads((tmp_path / "fraud_summary.json").read_text())
    assert set(summary["scenarios"]) == set(SCENARIOS)
    header = (tmp_path / "fraud_labels.csv").read_text().splitlines()[0]
    assert header == "transaction_id,scenario,incident_id,role,fraudulent"
    assert "scenarios (incidents/transactions): F1" in capsys.readouterr().out
