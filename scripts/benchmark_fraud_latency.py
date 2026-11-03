"""Benchmark of the hybrid fraud scorer's model step and its rules-only fallback rate (#362).

Real latency is hardware-dependent, so it is measured here, outside the test suite (no CI job runs
this). The script loads the served artifacts with the API's own loader (``load_engine``: manifest
checksums, warm-up), builds ``--calls`` seeded, realistic transfer inputs (amounts, 0-40 earlier
debits, payee ages, devices, failed sign-ins), and scores them through the API's own budgeted model
step (``score_within_budget``, the code ``HybridFraudScorer`` runs):

* ``unbounded``: the same calls with no budget, so p50/p95/p99 show the model step's real time;
* one pass per ``--budget-ms`` value: how many calls the budget cut off, i.e. the share of
  transfers that would have been scored by the rules alone (fail-safe, AGENTS.md section 8);
* ``anomaly``: one Isolation Forest row on its own (``ml/tests/test_fraud_training.py`` checks
  the forest's size, which bounds this cost; the time itself is measured here).

Calls are sequential. By default each budgeted call starts only once the model pool is idle again
(a timed-out call finishes in the background, as in the API), like transfers made one at a time
in a demo. ``--back-to-back`` skips that wait, so a slow call's leftover work competes with the
next one (a pessimistic stand-in for busy traffic). The load average is printed with each pass:
compare runs only at a similar load.

Usage (no database or Redis needed):
    uv run python scripts/benchmark_fraud_latency.py --model-dir models/fraud --calls 500 \\
        --budget-ms 50 --budget-ms 100 [--seed 42] [--back-to-back] [--json results.json]
Exit code 0 on a completed run, 2 if the artifacts cannot be loaded or a budget is invalid.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import sys
import threading
import time
import uuid
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from measure_analytics_latency import summarise

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)  # fixed: the inputs do not depend on the clock
NO_BUDGET_SECONDS = 60.0


def transfer_inputs(calls: int, seed: int) -> list[Any]:
    """``calls`` seeded transfers: the same seed always gives the same list."""
    from pbip_ml.fraud.features import PastTransfer
    from pbip_ml.fraud.inference import TransferInputs

    rng = random.Random(seed)  # noqa: S311 - reproducible benchmark inputs, not security
    devices = [uuid.UUID(int=rng.getrandbits(128)) for _ in range(8)]
    inputs = []
    for _ in range(calls):
        device = rng.choice(devices)
        history = [
            PastTransfer(
                NOW - timedelta(minutes=rng.randint(1, 30 * 24 * 60)),
                "DEBIT",
                rng.randint(100_00, 50_000_00),
                rng.choice(devices),
            )
            for _ in range(rng.randint(0, 40))
        ]
        payee_age = rng.choice([None, timedelta(minutes=rng.randint(1, 90 * 24 * 60))])
        inputs.append(
            TransferInputs(
                amount_minor=rng.randint(100_00, 2_00_000_00),
                currency="INR",
                occurred_at=NOW,
                payee_added_at=None if payee_age is None else NOW - payee_age,
                device_id=device,
                new_device=rng.random() < 0.1,
                failed_logins_24h=rng.choice([0, 0, 0, 0, 1, 2, 5]),
                history=history,
            )
        )
    return inputs


@dataclass(frozen=True)
class Pass:
    name: str
    budget_ms: int | None
    samples_ms: list[float]
    fallbacks: int
    load_before: tuple[float, float, float] | None
    load_after: tuple[float, float, float] | None

    def report(self) -> dict[str, Any]:
        calls = len(self.samples_ms)
        return {
            "pass": self.name,
            "budget_ms": self.budget_ms,
            "calls": calls,
            "fallbacks": self.fallbacks,
            "fallback_rate": round(self.fallbacks / calls, 4) if calls else 0.0,
            "wait_ms": summarise(self.samples_ms),
            "load_avg_1_5_15_before": _rounded(self.load_before),
            "load_avg_1_5_15_after": _rounded(self.load_after),
        }


def load_average() -> tuple[float, float, float] | None:
    """The 1/5/15-minute load average, or None where the OS has none (Windows: #371)."""
    getloadavg = getattr(os, "getloadavg", None)
    if getloadavg is None:
        return None
    try:
        load: tuple[float, float, float] = getloadavg()
    except OSError:  # the platform has the call but cannot read the load
        return None
    return load


def _rounded(load: tuple[float, float, float] | None) -> list[float] | None:
    return None if load is None else [round(x, 2) for x in load]


def wait_until_model_pool_is_idle() -> None:
    """Block until every worker of the API's model pool has finished its earlier calls."""
    from pbip_api.modules.fraud import hybrid

    workers = hybrid._POOL._max_workers  # the API's own pool: read here, never reconfigured
    barrier = threading.Barrier(workers + 1)
    for _ in range(workers):  # each worker runs one of these only after its earlier work
        hybrid._POOL.submit(barrier.wait, NO_BUDGET_SECONDS)
    barrier.wait(NO_BUDGET_SECONDS)


def run_pass(
    engine: Any, inputs: list[Any], name: str, budget_ms: int | None, isolated: bool = True
) -> Pass:
    """Time the budgeted wait of each call (about the budget for a call that timed out)."""
    from pbip_api.modules.fraud.hybrid import score_within_budget

    budget = NO_BUDGET_SECONDS if budget_ms is None else budget_ms / 1000
    load_before = load_average()
    samples, fallbacks = [], 0
    for item in inputs:
        started = time.perf_counter()
        try:
            result = score_within_budget(engine, item, budget)
            if result.model_version.endswith("+rules-only"):  # the models raised inside
                fallbacks += 1
        except FutureTimeout:
            fallbacks += 1
        samples.append((time.perf_counter() - started) * 1000)
        if isolated:
            wait_until_model_pool_is_idle()
    return Pass(name, budget_ms, samples, fallbacks, load_before, load_average())


def anomaly_pass(engine: Any, inputs: list[Any]) -> Pass:
    """One Isolation Forest row at a time, as the hybrid scorer calls it."""
    from pbip_ml.fraud.inference import feature_row

    rows = [feature_row(item)[0] for item in inputs]
    load_before = load_average()
    samples = []
    for row in rows:
        started = time.perf_counter()
        engine.anomaly.score(row)
        samples.append((time.perf_counter() - started) * 1000)
    return Pass("anomaly", None, samples, 0, load_before, load_average())


def format_line(report: dict[str, Any]) -> str:
    wait = report["wait_ms"]
    budget = "none" if report["budget_ms"] is None else f"{report['budget_ms']} ms"
    before = report["load_avg_1_5_15_before"]
    load = "unavailable" if before is None else " ".join(f"{x:.2f}" for x in before)
    return (
        f"{report['pass']:<10} budget={budget:<7} n={report['calls']} "
        f"p50={wait['median_ms']} p95={wait['p95_ms']} p99={wait['p99_ms']} "
        f"max={wait['max_ms']} ms "
        f"fallbacks={report['fallbacks']} ({report['fallback_rate']:.2%}) load={load}"
    )


def _load(model_dir: Path, budgets: list[int]) -> Any:
    from pbip_api.core.config import Settings
    from pbip_api.modules.fraud.hybrid import load_engine
    from pydantic import ValidationError

    try:  # the API's own validation: a budget the API would refuse is refused here too
        for budget in budgets:
            Settings(fraud_model_budget_ms=budget)
    except ValidationError as exc:
        sys.stderr.write(f"invalid --budget-ms: {exc.errors()[0]['msg']}\n")
        return None
    engine = load_engine(Settings(fraud_scorer="hybrid", fraud_model_dir=str(model_dir)))
    if engine is None:
        sys.stderr.write(f"could not load the fraud model from {model_dir}\n")
    return engine


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-dir", type=Path, default=Path("models/fraud"))
    parser.add_argument("--calls", type=int, default=500)
    parser.add_argument("--budget-ms", type=int, action="append", dest="budgets")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--back-to-back", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    budgets: list[int] = args.budgets or [50]

    engine = _load(args.model_dir, budgets)
    if engine is None:
        return 2
    import logging

    logging.disable(logging.CRITICAL)  # one fallback warning per timed-out call would add I/O
    inputs = transfer_inputs(args.calls, args.seed)
    passes = [run_pass(engine, inputs, "unbounded", None)]
    mode = "back2back" if args.back_to_back else "budget"
    passes += [
        run_pass(engine, inputs, mode, budget, isolated=not args.back_to_back) for budget in budgets
    ]
    passes.append(anomaly_pass(engine, inputs))
    reports = [p.report() for p in passes]
    result = {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model_dir": str(args.model_dir),
        "model_version": engine.params.model_version,
        "seed": args.seed,
        "cpus": os.cpu_count(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "passes": reports,
    }
    sys.stdout.write(
        f"model {result['model_version']} from {args.model_dir}, seed {args.seed}, "
        f"{result['cpus']} CPUs, {result['platform']}\n"
    )
    sys.stdout.write("\n".join(format_line(r) for r in reports) + "\n")
    if args.json:
        args.json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
