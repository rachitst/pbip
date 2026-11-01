"""TASK-023, TASK-071: labelled fraud scenarios F1-F8 are injected deterministically and cleanly."""

import json
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import pytest
from pbip_datagen.__main__ import main
from pbip_datagen.fraud import (
    DEFAULT_DORMANCY_DAYS,
    DEFAULT_MAX_TRANSFER_MINOR,
    DEFAULT_REPORTING_THRESHOLD_MINOR,
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


def test_f7_multiple_deposits_just_below_reporting_threshold(
    data: LabelledTransfers,
) -> None:
    incidents = _incidents(data, "F7")
    assert len(incidents) > 0
    for incident in incidents:
        assert 3 <= len(incident) <= 6
        for row, role in incident:
            assert role == "deposit"
            assert row.direction == "CREDIT"
            assert row.payee_added_at is None
            assert (
                0.89 * DEFAULT_REPORTING_THRESHOLD_MINOR
                <= row.amount_minor
                < DEFAULT_REPORTING_THRESHOLD_MINOR
            )
        total_inflow = sum(row.amount_minor for row, _ in incident)
        assert total_inflow > DEFAULT_REPORTING_THRESHOLD_MINOR
        assert len({row.counterparty_id for row, _ in incident}) == len(incident)


def test_f7_custom_reporting_threshold_is_respected() -> None:
    custom_threshold = 5_000_000  # INR 50,000.00
    cfg = FraudConfig(
        rates=dict.fromkeys(SCENARIOS, 0.05),
        reporting_threshold_minor=custom_threshold,
    )
    custom_data = generate_labelled(GeneratorConfig(seed=42, customers=200, months=3), cfg)
    incidents = _incidents(custom_data, "F7")
    assert len(incidents) > 0
    for incident in incidents:
        for row, role in incident:
            assert role == "deposit"
            assert row.direction == "CREDIT"
            assert 0.89 * custom_threshold <= row.amount_minor < custom_threshold


def test_f8_dormancy_greater_than_180_days_and_maximum_transfers(
    data: LabelledTransfers,
) -> None:
    by_customer: dict[object, list[Transfer]] = defaultdict(list)
    for t in data.transfers:
        by_customer[t.customer_id].append(t)

    incidents = _incidents(data, "F8")
    assert len(incidents) > 0
    for incident in incidents:
        assert 1 <= len(incident) <= 2
        f8_rows = [row for row, role in incident]
        customer_id = f8_rows[0].customer_id
        customer_txns = sorted(by_customer[customer_id], key=lambda t: t.occurred_at)

        for row, role in incident:
            assert role == "reactivation"
            assert row.direction == "DEBIT"
            assert row.amount_minor == DEFAULT_MAX_TRANSFER_MINOR
            assert row.new_device
            assert row.payee_added_at is not None
            assert row.occurred_at - row.payee_added_at < timedelta(hours=1)

        first_f8 = min(row.occurred_at for row in f8_rows)
        priors = [t for t in customer_txns if t.occurred_at < first_f8]
        assert priors, "Dormant account must have prior activity before dormancy"
        last_prior = priors[-1]
        gap = first_f8 - last_prior.occurred_at
        assert gap > timedelta(days=DEFAULT_DORMANCY_DAYS)

        in_gap = [t for t in customer_txns if last_prior.occurred_at < t.occurred_at < first_f8]
        assert in_gap == []


def test_f8_custom_dormancy_days_and_limit() -> None:
    custom_dormancy = 90
    custom_max_transfer = 15_000_000  # INR 1,50,000.00
    cfg = FraudConfig(
        rates=dict.fromkeys(SCENARIOS, 0.05),
        dormancy_days=custom_dormancy,
        max_transfer_minor=custom_max_transfer,
    )
    custom_data = generate_labelled(GeneratorConfig(seed=42, customers=200, months=6), cfg)
    by_customer: dict[object, list[Transfer]] = defaultdict(list)
    for t in custom_data.transfers:
        by_customer[t.customer_id].append(t)

    incidents = _incidents(custom_data, "F8")
    assert len(incidents) > 0
    for incident in incidents:
        f8_rows = [row for row, role in incident]
        customer_txns = sorted(by_customer[f8_rows[0].customer_id], key=lambda t: t.occurred_at)
        for row, role in incident:
            assert role == "reactivation"
            assert row.amount_minor == custom_max_transfer
        first_f8 = min(row.occurred_at for row in f8_rows)
        priors = [t for t in customer_txns if t.occurred_at < first_f8]
        assert priors
        assert first_f8 - priors[-1].occurred_at > timedelta(days=custom_dormancy)


def test_ordinary_transfers_never_match_a_scenario_signature(data: LabelledTransfers) -> None:
    labelled = {lb.transaction_id for lb in data.labels}
    for t in data.transfers:
        if t.id in labelled:
            continue
        assert t.direction == "DEBIT" and t.payee_added_at is not None
        assert t.occurred_at - t.payee_added_at >= timedelta(days=30)
        assert (t.ip_country, t.new_device, t.failed_logins_24h) == ("IN", False, 0)


def test_fraud_config_is_validated() -> None:
    with pytest.raises(ValueError, match=r"F1-F8"):
        FraudConfig(rates={"F1": 0.1})  # deliberately incomplete
    with pytest.raises(ValueError, match=r"at most 0\.5"):
        FraudConfig(rates=dict.fromkeys(SCENARIOS, 0.1))  # Literal keys
    with pytest.raises(ValueError, match=r"reporting_threshold_minor must be positive"):
        FraudConfig(reporting_threshold_minor=0)
    with pytest.raises(ValueError, match=r"max_transfer_minor must be positive"):
        FraudConfig(max_transfer_minor=-100)
    with pytest.raises(ValueError, match=r"dormancy_days must be positive"):
        FraudConfig(dormancy_days=0)


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
