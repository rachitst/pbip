"""Measure the F7 deposit-side structuring monitor on a synthetic export (#274, ADR-0052).

Usage (dev tool, not shipped)::

    uv run python -m pbip_datagen export --seed 42 --customers 3000 --months 6 --out data/fraud
    uv run python scripts/structuring_monitor_eval.py --data data/fraud [--json out.json]

It replays the served detection (``pbip_api.modules.fraud.structuring.find_patterns``, the
default ``FRAUD_STRUCTURING_*`` settings) as the hourly beat job runs it: one run at the first
hour boundary after each in-band credit, scanning ``LOOKBACK_WINDOWS`` windows back, with credits
already in an alert excluded. A pattern becomes visible exactly when its last credit arrives, so
these runs give the same alerts as running every hour. Each synthetic customer has one account.

Reported: F7 incidents flagged out of all F7 incidents; flagged legitimate credits out of all
legitimate credits (the false-positive rate per credit); alerts that hold no fraudulent credit and
the customers they name; and F6 (mule) inbound credits that happen to be flagged.
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pbip_api.modules.fraud.structuring import (
    LOOKBACK_WINDOWS,
    Credit,
    Pattern,
    StructuringBand,
    find_patterns,
)

DEFAULT_BAND = StructuringBand(10_000_000, 10, timedelta(hours=72), 3)  # core/config.py defaults
RUN_EVERY = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class Label:
    scenario: str
    incident_id: str
    fraudulent: bool


def next_run(at: datetime) -> datetime:
    """The first hourly run strictly after ``at`` (the job's own minute does not matter here)."""
    floor = at.replace(minute=0, second=0, microsecond=0)
    return floor + RUN_EVERY


def replay_hourly(credits: Sequence[Credit], band: StructuringBand) -> list[Pattern]:
    """The alerts the hourly job opens over this history, oldest first."""
    in_band = sorted(
        (c for c in credits if band.in_band(c.amount_minor)), key=lambda c: c.created_at
    )
    by_account: dict[object, list[Credit]] = defaultdict(list)
    for credit in in_band:
        by_account[credit.account_id].append(credit)
    alerts: list[Pattern] = []
    for rows in by_account.values():
        covered: set[object] = set()
        for run in sorted({next_run(c.created_at) for c in rows}):
            since = run - LOOKBACK_WINDOWS * band.window
            visible = [c for c in rows if since <= c.created_at < run]
            for pattern in find_patterns(visible, band, frozenset(covered)):
                alerts.append(pattern)
                covered.update(c.transaction_id for c in pattern.credits)
    return sorted(alerts, key=lambda p: (p.last.created_at, str(p.account_id)))


def measure(
    credits: Sequence[Credit], labels: dict[str, Label], band: StructuringBand = DEFAULT_BAND
) -> dict[str, Any]:
    alerts = replay_hourly(credits, band)
    flagged = {str(c.transaction_id) for p in alerts for c in p.credits}
    legit = [c for c in credits if not _fraudulent(labels, c)]
    flagged_legit = [c for c in legit if str(c.transaction_id) in flagged]
    f7 = {lb.incident_id for lb in labels.values() if lb.scenario == "F7" and lb.fraudulent}
    f7_hit = {
        labels[str(c.transaction_id)].incident_id
        for p in alerts
        for c in p.credits
        if str(c.transaction_id) in labels and labels[str(c.transaction_id)].scenario == "F7"
    }
    false_alerts = [p for p in alerts if not any(_fraudulent(labels, c) for c in p.credits)]
    f6 = [c for c in credits if _scenario(labels, c) == "F6"]
    accounts_with_legit = {c.account_id for c in legit}
    return {
        "alerts": len(alerts),
        "f7_incidents": len(f7),
        "f7_incidents_flagged": len(f7_hit),
        "f7_recall": _ratio(len(f7_hit), len(f7)),
        "legitimate_credits": len(legit),
        "legitimate_credits_flagged": len(flagged_legit),
        "legitimate_credit_fpr": _ratio(len(flagged_legit), len(legit)),
        "false_alerts": len(false_alerts),
        "accounts_with_legitimate_credits": len(accounts_with_legit),
        "legitimate_accounts_falsely_alerted": len({p.account_id for p in false_alerts}),
        "f6_inbound_credits": len(f6),
        "f6_inbound_credits_flagged": sum(str(c.transaction_id) in flagged for c in f6),
    }


def _fraudulent(labels: dict[str, Label], credit: Credit) -> bool:
    label = labels.get(str(credit.transaction_id))
    return label is not None and label.fraudulent


def _scenario(labels: dict[str, Label], credit: Credit) -> str | None:
    label = labels.get(str(credit.transaction_id))
    return label.scenario if label is not None else None


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 6) if whole else None


def read_export(directory: Path) -> tuple[list[Credit], dict[str, Label]]:
    """CREDIT rows of ``transfers.csv`` (each customer is one account) and ``fraud_labels.csv``."""
    with (directory / "transfers.csv").open(encoding="utf-8", newline="") as handle:
        credits = [
            Credit(
                row["id"], row["customer_id"], datetime.fromisoformat(row["occurred_at"]),
                int(row["amount_minor"]),
            )
            for row in csv.DictReader(handle)
            if row["direction"] == "CREDIT"
        ]  # fmt: skip
    with (directory / "fraud_labels.csv").open(encoding="utf-8", newline="") as handle:
        labels = {
            row["transaction_id"]: Label(
                row["scenario"], row["incident_id"], row["fraudulent"] == "True"
            )
            for row in csv.DictReader(handle)
        }
    return credits, labels


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, required=True, help="a pbip_datagen export directory")
    parser.add_argument("--json", type=Path, help="also write the result here")
    args = parser.parse_args(list(argv) if argv is not None else None)
    credits, labels = read_export(args.data)
    result = {"data": str(args.data), **measure(credits, labels)}
    text = json.dumps(result, indent=2)
    sys.stdout.write(text + "\n")
    if args.json is not None:
        args.json.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
