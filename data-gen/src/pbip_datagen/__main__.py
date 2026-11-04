"""Command line: ``python -m pbip_datagen {load,export,uptake} --seed 42 --customers 1000 ...``.

``load`` generates a dataset and COPYs it into the ``synthetic`` schema; ``export`` writes the same
dataset as CSV files for offline analysis; ``uptake`` writes the SIMULATED product-uptake panel
(``uptake.py``, uptake-sim-2) that the recommendation ranker experiment trains on. The database
URL comes from ``--database-url`` or the ``DATABASE_URL`` environment variable (developer
tooling only; never logged).
"""

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import astuple, fields
from datetime import date
from pathlib import Path

from pbip_datagen.fraud import FraudLabel, Transfer, generate_labelled
from pbip_datagen.generator import (
    GeneratorConfig,
    SyntheticCustomer,
    SyntheticDataset,
    SyntheticTransaction,
    generate,
)
from pbip_datagen.loader import load_dataset
from pbip_datagen.uptake import (
    PRODUCTS,
    PanelRow,
    UptakeAssumptions,
    perturbed,
    simulate_panel,
)
from pbip_datagen.uptake import summary as uptake_summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m pbip_datagen", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("load", "COPY into PostgreSQL"), ("export", "write CSV files")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--seed", type=int, default=42)
        command.add_argument("--customers", type=int, default=1000)
        command.add_argument("--months", type=int, default=12)
        command.add_argument("--start", type=date.fromisoformat, default=date(2025, 1, 1))
    sub.choices["load"].add_argument("--database-url", default=None)
    sub.choices["export"].add_argument("--out", type=Path, required=True)
    uptake = sub.add_parser("uptake", help="write the simulated product-uptake panel (CSV)")
    uptake.add_argument("--seed", type=int, default=42)
    uptake.add_argument("--customers", type=int, default=3000)
    uptake.add_argument("--months", type=int, default=24)
    uptake.add_argument("--out", type=Path, required=True)
    uptake.add_argument("--effect-scale", type=float, default=1.0, help="held-out variant")
    uptake.add_argument("--base-shift", type=float, default=0.0, help="held-out variant")
    return parser


UPTAKE_PANEL_FILE = "uptake_panel.csv"
UPTAKE_SUMMARY_FILE = "uptake_summary.json"


def _panel_columns() -> list[str]:
    scalar = [f.name for f in fields(PanelRow) if f.name not in ("clicks", "took")]
    return [*scalar, *(f"clicks_{p}" for p in PRODUCTS), *(f"took_{p}" for p in PRODUCTS)]


def export_uptake(
    seed: int,
    customers: int,
    months: int,
    out: Path,
    effect_scale: float = 1.0,
    base_shift: float = 0.0,
) -> dict[str, object]:
    """The panel as CSV (money in paise) and its summary. Labels are SIMULATED (uptake.py)."""
    assumptions = perturbed(UptakeAssumptions(), effect_scale, base_shift)
    rows = simulate_panel(seed, customers, months, assumptions)
    out.mkdir(parents=True, exist_ok=True)
    _write_csv(
        out / UPTAKE_PANEL_FILE,
        _panel_columns(),
        [(*astuple(row)[:-2], *row.clicks, *row.took) for row in rows],
    )
    summary = {
        "seed": seed,
        "customers": customers,
        "months": months,
        "effect_scale": effect_scale,
        "base_shift": base_shift,
        **uptake_summary(rows),
    }
    (out / UPTAKE_SUMMARY_FILE).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def _write_csv(path: Path, header: list[str], rows: list[tuple[object, ...]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _export_fraud(config: GeneratorConfig, out: Path) -> dict[str, object]:
    """v2: transfers with context, fraud labels and the per-scenario counts."""
    labelled = generate_labelled(config)
    _write_csv(
        out / "transfers.csv",
        [f.name for f in fields(Transfer)],
        [astuple(t) for t in labelled.transfers],
    )
    _write_csv(
        out / "fraud_labels.csv",
        [f.name for f in fields(FraudLabel)],
        [astuple(lb) for lb in labelled.labels],
    )
    summary = labelled.summary()
    (out / "fraud_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def _export(dataset: SyntheticDataset, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    _write_csv(
        out / "customers.csv",
        [f.name for f in fields(SyntheticCustomer)],
        [astuple(c) for c in dataset.customers],
    )
    _write_csv(
        out / "transactions.csv",
        [f.name for f in fields(SyntheticTransaction)],
        [astuple(t) for t in dataset.transactions],
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "uptake":
        if args.customers < 1 or args.months < 2:
            sys.stderr.write("error: need at least 1 customer and 2 months\n")
            return 2
        result = export_uptake(
            args.seed,
            args.customers,
            args.months,
            args.out,
            args.effect_scale,
            args.base_shift,
        )
        sys.stdout.write(
            f"wrote {result['customer_months']} simulated customer-months to {args.out}\n"
        )
        return 0
    try:
        config = GeneratorConfig(args.seed, args.customers, args.months, args.start)
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    started = time.perf_counter()
    dataset = generate(config)
    generated_in = time.perf_counter() - started
    if args.command == "export":
        _export(dataset, args.out)
        summary = _export_fraud(config, args.out)
        scenarios = summary["scenarios"]
        assert isinstance(scenarios, dict)
        counts = ", ".join(
            f"{s} {v['incidents']}/{v['transactions']}" for s, v in scenarios.items()
        )
        sys.stdout.write(
            f"wrote {len(dataset.transactions)} transactions, {summary['transfers']} transfers and "
            f"{summary['fraud_transactions']} fraud labels to {args.out}\n"
            f"scenarios (incidents/transactions): {counts}\n"
        )
        return 0
    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        sys.stderr.write("error: pass --database-url or set DATABASE_URL\n")
        return 2
    report = load_dataset(dataset, database_url)
    sys.stdout.write(
        f"run {dataset.run_id}: {report.customers} customers, {report.transactions} transactions; "
        f"generated in {generated_in:.1f}s, COPY-loaded in {report.seconds:.1f}s, "
        f"total {generated_in + report.seconds:.1f}s\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
