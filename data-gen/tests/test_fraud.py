"""TASK-023, TASK-071: labelled fraud scenarios F1-F8 are injected deterministically and cleanly."""

import hashlib
import json
from collections import defaultdict
from dataclasses import fields
from datetime import timedelta
from pathlib import Path

import pytest
from pbip_datagen.__main__ import main
from pbip_datagen.calendars import at_ist
from pbip_datagen.fraud import (
    DEFAULT_DORMANCY_DAYS,
    DEFAULT_MAX_TRANSFER_MINOR,
    DEFAULT_REPORTING_THRESHOLD_MINOR,
    SCENARIOS,
    FraudConfig,
    FraudLabel,
    LabelledTransfers,
    Transfer,
    customer_transfers,
    generate_labelled,
)
from pbip_datagen.generator import GeneratorConfig, customer_transactions, iter_customers

CONFIG = GeneratorConfig(seed=7, customers=3000, months=6)
# F8 needs more than 180 days of dormancy inside the data, so it only appears in longer runs.
LONG_CONFIG = GeneratorConfig(seed=11, customers=500, months=12)
# SHA-256 of every row and label except F7/F8 ones, recorded by ``_v2_1_digest`` on the output of
# generator v2.1.0 (main at 2e65b87, before TASK-071). TASK-071 must be additive: if any F1-F6,
# ordinary or legitimate first-time (L1-L3) row or label changes, these stop matching. The one
# exception: an F8 customer's ordinary transfers dated after churn (a v2.0 artefact; a dormant
# account has none) are skipped. CONFIG has no F8, so its digest has no exception at all.
V2_1_DIGESTS = {
    CONFIG: "dcdba4f908ae10108299272f257620aab538d6030430527fed8736c5af893d72",  # pragma: allowlist secret (a SHA-256 digest of data)
    LONG_CONFIG: "f94e398fe0c08806ed4de080691c3e9ec39aa6abf245bb4cc3f7ceb7f9fdd696",  # pragma: allowlist secret (a SHA-256 digest of data)
}


@pytest.fixture(scope="module")
def data() -> LabelledTransfers:
    return generate_labelled(CONFIG)


@pytest.fixture(scope="module")
def long_data() -> LabelledTransfers:
    return generate_labelled(LONG_CONFIG)


def _row(record: Transfer | FraudLabel) -> bytes:
    """``repr(dataclasses.astuple(record))`` without astuple's deep copy (10x faster)."""
    return repr(tuple(getattr(record, f.name) for f in fields(record))).encode() + b"\n"


def _v2_1_digest(data: LabelledTransfers, config: GeneratorConfig) -> str:
    new = {lb.transaction_id for lb in data.labels if lb.scenario in ("F7", "F8")}
    customer_of = {t.id: t.customer_id for t in data.transfers}
    f8 = {customer_of[lb.transaction_id] for lb in data.labels if lb.scenario == "F8"}
    churned = {c.id: at_ist(c.churned_on, 0) for c in iter_customers(config) if c.churned_on}
    digest = hashlib.sha256()
    for t in data.transfers:
        if t.customer_id in f8 and t.occurred_at >= churned[t.customer_id]:
            continue
        if t.id not in new:
            digest.update(_row(t))
    digest.update(b"--labels--\n")
    for lb in data.labels:
        if lb.transaction_id not in new:
            digest.update(_row(lb))
    return digest.hexdigest()


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


def test_v2_1_rows_are_unchanged(data: LabelledTransfers, long_data: LabelledTransfers) -> None:
    """TASK-071 adds F7/F8 without changing one byte of any v2.1 row or label for these seeds."""
    assert _v2_1_digest(data, CONFIG) == V2_1_DIGESTS[CONFIG]
    assert _v2_1_digest(long_data, LONG_CONFIG) == V2_1_DIGESTS[LONG_CONFIG]
    assert _incidents(long_data, "F7") and _incidents(long_data, "F8")  # the digest skipped them


def test_a_customer_has_at_most_one_incident(long_data: LabelledTransfers) -> None:
    by_id = {t.id: t.customer_id for t in long_data.transfers}
    scenarios: dict[object, set[str]] = defaultdict(set)
    for lb in long_data.labels:
        scenarios[by_id[lb.transaction_id]].add(lb.scenario)
    assert all(len(found) == 1 for found in scenarios.values())


def test_every_scenario_is_present_and_the_summary_counts_the_labels(
    data: LabelledTransfers, long_data: LabelledTransfers
) -> None:
    summary = data.summary()
    scenarios = summary["scenarios"]
    assert isinstance(scenarios, dict)
    for scenario in SCENARIOS:
        incidents = _incidents(data, scenario)
        assert len(incidents) == scenarios[scenario]["incidents"]
        assert (len(incidents) > 0) is (scenario != "F8")  # 6 months cannot hold 180 dormant days
    long_scenarios = long_data.summary()["scenarios"]
    assert isinstance(long_scenarios, dict)
    assert long_scenarios["F8"]["incidents"] == len(_incidents(long_data, "F8")) > 0
    assert summary["fraud_transactions"] == sum(lb.fraudulent for lb in data.labels)
    ids = {t.id for t in data.transfers}
    assert all(lb.transaction_id in ids for lb in data.labels)  # every label points at a row
    assert len({lb.transaction_id for lb in data.labels}) == len(data.labels)


def test_rates_are_honoured(data: LabelledTransfers) -> None:
    rates = FraudConfig().rates
    for scenario in SCENARIOS[:7]:  # F8's rate is per dormant account: see the F8 tests
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


def test_f7_structuring_deposits_just_below_the_reporting_threshold(
    data: LabelledTransfers,
) -> None:
    threshold = DEFAULT_REPORTING_THRESHOLD_MINOR
    incidents = _incidents(data, "F7")
    assert len(incidents) >= 10
    for incident in incidents:
        rows = [row for row, _ in incident]
        assert 3 <= len(rows) <= 6
        assert {role for _, role in incident} == {"deposit"}
        assert all(r.direction == "CREDIT" and r.payee_added_at is None for r in rows)
        assert len({r.counterparty_id for r in rows}) == len(rows)  # several senders
        for r in rows:  # each just beneath: 90-99.5% of the threshold, in round 0.5% steps
            assert 0.9 * threshold <= r.amount_minor < threshold
            assert r.amount_minor % (threshold // 200) == 0
        assert sum(r.amount_minor for r in rows) > 2.5 * threshold  # together well above it
        times = sorted(r.occurred_at for r in rows)
        assert times[-1] - times[0] < timedelta(days=3)  # clustered in one window
    labels = [lb for lb in data.labels if lb.scenario == "F7"]
    assert all(lb.fraudulent for lb in labels)


def test_f7_follows_a_configured_threshold() -> None:
    rates = {**FraudConfig().rates, "F7": 0.2}
    fraud = FraudConfig(rates=rates, reporting_threshold_minor=50_000_00)
    custom = generate_labelled(GeneratorConfig(seed=5, customers=200, months=3), fraud)
    incidents = _incidents(custom, "F7")
    assert len(incidents) >= 20
    for incident in incidents:
        assert all(45_000_00 <= row.amount_minor < 50_000_00 for row, _ in incident)


def _check_f8_incidents(labelled: LabelledTransfers, config: GeneratorConfig,
                        fraud: FraudConfig) -> int:  # fmt: skip
    """Every F8 incident: dormant > dormancy_days across ALL activity, then maximum transfers."""
    customers = {c.id: c for c in iter_customers(config)}
    transfers: dict[object, list[Transfer]] = defaultdict(list)
    for t in labelled.transfers:
        transfers[t.customer_id].append(t)
    incidents = _incidents(labelled, "F8")
    for incident in incidents:
        rows = sorted((row for row, _ in incident), key=lambda r: r.occurred_at)
        assert 1 <= len(rows) <= 2 and {role for _, role in incident} == {"reactivation"}
        assert len({r.counterparty_id for r in rows}) == 1  # one new beneficiary is drained
        for r in rows:
            assert r.direction == "DEBIT" and r.amount_minor == fraud.max_transfer_minor
            assert r.new_device and r.payee_added_at is not None
            assert r.occurred_at - r.payee_added_at < timedelta(hours=1)
        customer = customers[rows[0].customer_id]
        assert customer.churned_on is not None  # the customer had gone inactive
        incident_ids = {r.id for r in rows}
        earlier = [t.occurred_at for t in transfers[customer.id] if t.id not in incident_ids]
        earlier += [t.occurred_at for t in customer_transactions(config, customer)]
        assert earlier and max(earlier) < rows[0].occurred_at
        assert rows[0].occurred_at - max(earlier) > timedelta(days=fraud.dormancy_days)
    return len(incidents)


def test_f8_dormant_account_suddenly_moves_the_maximum(long_data: LabelledTransfers) -> None:
    assert _check_f8_incidents(long_data, LONG_CONFIG, FraudConfig()) > 0
    assert DEFAULT_DORMANCY_DAYS == 180 and DEFAULT_MAX_TRANSFER_MINOR == 2_00_000_00
    assert all(lb.fraudulent for lb in long_data.labels if lb.scenario == "F8")


def test_f8_rate_is_the_share_of_dormant_accounts(long_data: LabelledTransfers) -> None:
    """Only accounts dormant long enough within the data qualify; about 20% of them are hit."""
    last_day = (LONG_CONFIG.start.replace(year=LONG_CONFIG.start.year + 1)
                - timedelta(days=5))  # fmt: skip  # day 27 of the last month, Dec 2025
    dormant = {
        c.id
        for c in iter_customers(LONG_CONFIG)
        if c.churned_on and c.churned_on + timedelta(days=DEFAULT_DORMANCY_DAYS + 2) <= last_day
    }
    by_id = {t.id: t.customer_id for t in long_data.transfers}
    hit = {by_id[lb.transaction_id] for lb in long_data.labels if lb.scenario == "F8"}
    assert hit <= dormant
    expected = FraudConfig().rates["F8"] * len(dormant)
    assert 0.4 * expected <= len(hit) <= 1.6 * expected


def test_f8_follows_configured_dormancy_and_maximum() -> None:
    config = GeneratorConfig(seed=3, customers=300, months=14)
    fraud = FraudConfig(dormancy_days=240, max_transfer_minor=1_50_000_00)
    labelled = generate_labelled(config, fraud)
    assert _check_f8_incidents(labelled, config, fraud) > 0


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
    with pytest.raises(ValueError, match=r"F1-F8"):
        FraudConfig(rates={"F1": 0.1})  # type: ignore[dict-item]  # deliberately incomplete
    with pytest.raises(ValueError, match=r"at most 0\.5"):
        FraudConfig(rates=dict.fromkeys(SCENARIOS, 0.1))  # type: ignore[arg-type]  # Literal keys
    with pytest.raises(ValueError, match=r"reporting_threshold_minor"):
        FraudConfig(reporting_threshold_minor=19_999)
    with pytest.raises(ValueError, match=r"max_transfer_minor"):
        FraudConfig(max_transfer_minor=0)
    for days in (0, 179, 3651):  # "dormant for > 180 days" is the floor
        with pytest.raises(ValueError, match=r"dormancy_days"):
            FraudConfig(dormancy_days=days)


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
