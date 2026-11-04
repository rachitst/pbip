"""Benchmark of the synthetic dataset's COPY load (TASK-009; ADR-0009: 1,000 customers < 15 s).

Real load time is hardware-dependent, so it is measured here, outside the test suite (no CI job runs
this). ``test_copy_loads_1000_customers_with_one_copy_per_table`` asserts the load's shape instead
(one COPY per table, ANALYZE first, a set-based trigger). The script migrates a scratch database to
head, generates the seeded dataset once, then times ``--runs`` fresh loads with the loader's own
``LoadReport.seconds``. Before each run the previous run's rows are deleted (untimed), so every
run loads into an empty ``synthetic`` schema, as the test did. The load average is printed before
and after: compare runs only at a similar load.

Usage (a scratch database only: its name must start with ``pbip_test`` or ``pbip_bench``):
    uv run python scripts/benchmark_synthetic_load.py \\
        --database-url "$BENCH_DATABASE_URL" \\
        [--customers 1000] [--months 12] [--seed 42] [--runs 3] [--json results.json]
Exit code 0 on a completed run (whatever the time), 2 if the database is not a scratch database.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
SCRATCH_PREFIXES = ("pbip_test", "pbip_bench")
BUDGET_SECONDS = 15.0  # ADR-0009 / TASK-009, for 1,000 customers over 12 months


def scratch_database_url(url: str) -> str:
    """``url`` if it names a scratch database; the benchmarks write to it, so never the demo DB."""
    name = make_url(url).database or ""
    if not name.startswith(SCRATCH_PREFIXES):
        msg = f"refusing database {name!r}: use a scratch database named {SCRATCH_PREFIXES[0]}*"
        raise ValueError(msg)
    return url


def migrate(url: str) -> None:
    """Bring the scratch database to head (idempotent), as the test harness does."""
    from alembic import command
    from alembic.config import Config

    os.environ["ALEMBIC_DATABASE_URL"] = url
    command.upgrade(Config(str(ROOT / "apps" / "api" / "alembic.ini")), "head")


def load_average() -> str:
    return " / ".join(f"{v:.2f}" for v in os.getloadavg()) if hasattr(os, "getloadavg") else "n/a"


def environment() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpus": os.cpu_count(),
        "load_average": load_average(),
    }


def _delete_run(url: str, run_id: object) -> None:
    import psycopg
    from pbip_datagen.loader import to_conninfo

    with psycopg.connect(to_conninfo(url)) as connection:
        connection.execute("DELETE FROM synthetic.generation_runs WHERE id = %s", (run_id,))


def summary(seconds: list[float]) -> dict[str, Any]:
    return {
        "runs": len(seconds),
        "seconds": [round(s, 2) for s in seconds],
        "median_s": round(statistics.median(seconds), 2),
        "max_s": round(max(seconds), 2),
        "within_budget": sum(s < BUDGET_SECONDS for s in seconds),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--customers", type=int, default=1000)
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    try:
        url = scratch_database_url(args.database_url)
    except ValueError as error:
        sys.stderr.write(f"{error}\n")
        return 2

    from pbip_datagen.generator import GeneratorConfig, generate
    from pbip_datagen.loader import load_dataset

    migrate(url)
    started = time.perf_counter()
    dataset = generate(
        GeneratorConfig(seed=args.seed, customers=args.customers, months=args.months)
    )
    generated_s = time.perf_counter() - started
    before = environment()
    seconds = []
    for _ in range(args.runs):
        _delete_run(url, dataset.run_id)  # untimed: every run loads into an empty schema
        seconds.append(load_dataset(dataset, url).seconds)
    _delete_run(url, dataset.run_id)
    result = {
        "customers": args.customers,
        "months": args.months,
        "transactions": len(dataset.transactions),
        "generate_s": round(generated_s, 2),
        "budget_s": BUDGET_SECONDS,
        **summary(seconds),
        "environment_before": before,
        "load_average_after": load_average(),
    }
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    if args.json:
        args.json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
