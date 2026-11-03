"""#274 / ADR-0052: the offline replay of the deposit monitor over a synthetic export."""

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pbip_api.modules.fraud.structuring import Credit

from scripts import structuring_monitor_eval as ev

pytestmark = pytest.mark.unit

T0 = datetime(2025, 3, 1, 9, 10, tzinfo=UTC)
NEAR = 9_950_000


def credit(tid: str, account: str, hours: float, amount: int = NEAR) -> Credit:
    return Credit(tid, account, T0 + timedelta(hours=hours), amount)


def test_replay_alerts_when_the_last_credit_arrives_and_never_twice() -> None:
    rows = [credit(f"s{i}", "S", h) for i, h in enumerate((0, 10, 20, 30, 40))]
    alerts = ev.replay_hourly(rows, ev.DEFAULT_BAND)
    # The run after the third credit alerts on three; the fourth and fifth are one short.
    assert [[c.transaction_id for c in a.credits] for a in alerts] == [["s0", "s1", "s2"]]


def test_credits_arriving_before_the_same_run_are_alerted_together() -> None:
    rows = [credit(f"s{i}", "S", h) for i, h in enumerate((0, 1, 2.1, 2.5))]
    alerts = ev.replay_hourly(rows, ev.DEFAULT_BAND)
    assert [len(a.credits) for a in alerts] == [4]  # s2 (11:16) and s3 (11:40): the 12:00 run


def test_measure_reports_recall_and_false_positives_per_credit_and_account() -> None:
    labels = {
        **{f"f{i}": ev.Label("F7", "inc-1", True) for i in range(3)},
        **{f"m{i}": ev.Label("F6", "inc-2", True) for i in range(2)},
    }
    credits = [
        *(credit(f"f{i}", "F", i) for i in range(3)),  # F7: flagged
        *(credit(f"m{i}", "M", i, 3_000_000) for i in range(2)),  # F6: out of band
        *(credit(f"b{i}", "B", 5 * i) for i in range(3)),  # a legitimate business: false alert
        credit("p0", "P", 0, 5_000_000),  # an ordinary legitimate credit
    ]
    result = ev.measure(credits, labels)
    assert result == {
        "alerts": 2,
        "f7_incidents": 1,
        "f7_incidents_flagged": 1,
        "f7_recall": 1.0,
        "legitimate_credits": 4,
        "legitimate_credits_flagged": 3,
        "legitimate_credit_fpr": 0.75,
        "false_alerts": 1,
        "accounts_with_legitimate_credits": 2,
        "legitimate_accounts_falsely_alerted": 1,
        "f6_inbound_credits": 2,
        "f6_inbound_credits_flagged": 0,
    }


def test_main_reads_an_export_directory(tmp_path: Path) -> None:
    header = ["id", "customer_id", "occurred_at", "direction", "amount_minor"]
    rows = [[f"f{i}", "F", (T0 + timedelta(hours=i)).isoformat(), "CREDIT", NEAR] for i in range(3)]
    rows.append(["d0", "F", T0.isoformat(), "DEBIT", NEAR])  # debits are not deposits
    with (tmp_path / "transfers.csv").open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows([header, *rows])
    with (tmp_path / "fraud_labels.csv").open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(
            [["transaction_id", "scenario", "incident_id", "role", "fraudulent"]]
            + [[f"f{i}", "F7", "inc", "deposit", "True"] for i in range(3)]
        )
    out = tmp_path / "result.json"
    assert ev.main(["--data", str(tmp_path), "--json", str(out)]) == 0
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["f7_incidents_flagged"] == 1 and result["legitimate_credits"] == 0
    assert result["legitimate_credit_fpr"] is None  # no legitimate credits: not zero, unknown
