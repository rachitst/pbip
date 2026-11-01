"""TASK-072 spike: evaluate circular fund loop detection (NetworkX) and measure its cost.

Offline tooling only; results are written up in ``docs/ml/circular-transfer-spike.md``.

    uv run python scripts/evaluate_circular_rings.py benchmark   # hand-built rings + distractors
    uv run python scripts/evaluate_circular_rings.py generator   # real generator size (seed 42)

``generator`` builds the same transfers as ``make datagen`` (seed 42, 1,000 customers, 12 months)
in memory - no database - then:

1. runs detection on the untouched data (every alert is a false positive);
2. plants labelled rings between existing customers, ADDITIVELY in this script (the generator and
   the training data are unchanged): positives (3-5 hops, >90% retained, closes in < 7 days) and
   negatives the detector must ignore (leaks > 10%, 2-cycles, 6-cycles, too slow, time-reversed);
3. a pessimistic stress run where every ordinary transfer is rewired to another customer, so the
   graph is dense with customer-to-customer cycles (the real data has none, see the doc).

Detection runs as a weekly batch: a 14-day lookback graph every 7 days, alerts unioned.
"""

import argparse
import json
import sys
import time
import tracemalloc
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid5

import networkx as nx
import numpy as np
from pbip_datagen.fraud import Transfer, generate_labelled
from pbip_datagen.generator import GeneratorConfig
from pbip_ml.fraud.circular import (
    DEFAULT_MAX_CYCLE_LENGTH,
    TransferLike,
    TransferRow,
    build_transaction_graph,
    calculate_risk_boosts,
    detect_circular_loops,
    evaluate_circular_detection,
    generate_circular_ring_evaluation_dataset,
)

LOOKBACK = timedelta(days=14)
STEP = timedelta(days=7)
NegativeKind = Literal["leaky", "two_cycle", "six_cycle", "slow", "reversed"]
NEGATIVE_KINDS: tuple[NegativeKind, ...] = ("leaky", "two_cycle", "six_cycle", "slow", "reversed")


def _out(line: str = "") -> None:
    sys.stdout.write(line + "\n")


@dataclass(frozen=True)
class WindowRun:
    alerts: set[frozenset[UUID]]
    windows: int
    seconds: float
    cpu_seconds: float
    slowest_window_ms: float
    max_nodes: int
    max_edges: int
    max_window_cycles: int


def run_windows(
    rows: Sequence[TransferLike], start: datetime, end: datetime, *, count_cycles: bool = False
) -> WindowRun:
    """Weekly batch: detect on each 14-day lookback graph, union the alerts (ring node sets).

    ``count_cycles`` also counts the bounded cycles NetworkX enumerates per window (outside the
    timed section's purpose, so only enable it for the stress run; it adds the same work again).
    """
    alerts: set[frozenset[UUID]] = set()
    slowest, max_nodes, max_edges, windows, max_cycles = 0.0, 0, 0, 0, 0
    t0, c0 = time.perf_counter(), time.process_time()
    as_of = start + LOOKBACK
    while as_of < end + STEP:
        w0 = time.perf_counter()
        graph = build_transaction_graph(rows, lookback_window=LOOKBACK, as_of=as_of)
        alerts |= {loop.node_set for loop in detect_circular_loops(graph)}
        slowest = max(slowest, (time.perf_counter() - w0) * 1000)
        max_nodes = max(max_nodes, graph.number_of_nodes())
        max_edges = max(max_edges, graph.number_of_edges())
        if count_cycles:
            bounded = nx.simple_cycles(graph, length_bound=DEFAULT_MAX_CYCLE_LENGTH)
            max_cycles = max(max_cycles, sum(1 for _ in bounded))
        windows += 1
        as_of += STEP
    wall, cpu = time.perf_counter() - t0, time.process_time() - c0
    return WindowRun(alerts, windows, wall, cpu, slowest, max_nodes, max_edges, max_cycles)


class Planter:
    """Seeded, additive ring injection between distinct existing customers."""

    def __init__(self, customers: list[UUID], start: datetime, end: datetime, seed: int) -> None:
        self._rng = np.random.default_rng([seed, 72])
        self._pool = [customers[i] for i in self._rng.permutation(len(customers))]
        self._start, self._end = start, end
        self._n = 0
        self.rows: list[TransferRow] = []

    def _members(self, k: int) -> list[UUID]:
        members, self._pool = self._pool[:k], self._pool[k:]
        if len(members) < k:
            raise ValueError("not enough distinct customers to plant every ring")
        return members

    def _hop(self, src: UUID, tgt: UUID, amount: int, when: datetime) -> None:
        self._n += 1
        tx_id = uuid5(UUID(int=0x72), f"planted-{self._n}")
        self.rows.append(TransferRow(tx_id, src, tgt, "DEBIT", amount, when))

    def ring(self, k: int, loss: tuple[float, float], gap_h: tuple[float, float],
             *, reverse: bool = False) -> frozenset[UUID]:  # fmt: skip
        members = self._members(k)
        span = (self._end - self._start - timedelta(days=21)).total_seconds()
        when = self._start + timedelta(days=7, seconds=float(self._rng.uniform(0, span)))
        amount = int(self._rng.integers(50_000, 500_000)) * 100  # INR 50k-5L, whole rupees
        times, amounts = [], []
        for _ in range(k):
            times.append(when)
            amounts.append(amount)
            when += timedelta(hours=float(self._rng.uniform(*gap_h)))
            amount = int(amount * (1 - float(self._rng.uniform(*loss))))
        if reverse:
            times.reverse()
        for i in range(k):
            self._hop(members[i], members[(i + 1) % k], amounts[i], times[i])
        return frozenset(members)

    def negative(self, kind: NegativeKind) -> frozenset[UUID]:
        if kind == "leaky":  # 3 hops, 6-12% lost per hop: 77-88% retained
            return self.ring(3, (0.06, 0.12), (1, 36))
        if kind == "two_cycle":
            return self.ring(2, (0.005, 0.02), (1, 36))
        if kind == "six_cycle":
            return self.ring(6, (0.005, 0.02), (1, 36))
        if kind == "slow":  # 5 hops 3.5-4.5 days apart: each hop < 7 days, loop > 14 days
            return self.ring(5, (0.005, 0.02), (84, 108))
        return self.ring(3, (0.005, 0.02), (1, 36), reverse=True)


def rewire(rows: Sequence[Transfer], customers: list[UUID], seed: int) -> list[Transfer]:
    """Stress variant: every counterparty becomes a random other customer (dense cycles)."""
    rng = np.random.default_rng([seed, 73])
    picks = rng.integers(0, len(customers), len(rows))
    out = []
    for row, pick in zip(rows, picks, strict=True):
        other = customers[int(pick)]
        if other == row.customer_id:
            other = customers[(int(pick) + 1) % len(customers)]
        out.append(replace(row, counterparty_id=other))
    return out


def _measure[T](fn: Callable[[], T]) -> tuple[T, float]:
    """Run ``fn`` under tracemalloc and return (result, peak MiB of Python allocations)."""
    tracemalloc.start()
    try:
        result = fn()
        peak = tracemalloc.get_traced_memory()[1] / 2**20
    finally:
        tracemalloc.stop()
    return result, peak


def _peak_rss_mib() -> float | None:
    """Peak resident set size of this process (Linux/macOS; ``None`` on Windows)."""
    try:
        import resource
    except ImportError:
        return None
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def _report_run(name: str, run: WindowRun) -> dict[str, object]:
    _out(f"[{name}] {run.windows} weekly windows in {run.seconds:.2f} s wall, "
         f"{run.cpu_seconds:.2f} s CPU (slowest window {run.slowest_window_ms:.0f} ms, "
         f"largest graph "
         f"{run.max_nodes} nodes / {run.max_edges} edges), alerts: {len(run.alerts)}")  # fmt: skip
    return {
        "windows": run.windows,
        "seconds": round(run.seconds, 3),
        "cpu_seconds": round(run.cpu_seconds, 3),
        "slowest_window_ms": round(run.slowest_window_ms, 1),
        "max_nodes": run.max_nodes,
        "max_edges": run.max_edges,
        "alerts": len(run.alerts),
    }


def run_generator(seed: int, customers: int, months: int, rings: int) -> dict[str, object]:
    config = GeneratorConfig(seed=seed, customers=customers, months=months, start=date(2025, 1, 1))
    t0 = time.perf_counter()
    normal = list(generate_labelled(config).transfers)
    _out(f"Generated {len(normal):,} transfers for {customers:,} customers "
         f"x {months} months (seed {seed}) in {time.perf_counter() - t0:.1f} s")  # fmt: skip
    start = min(t.occurred_at for t in normal)
    end = max(t.occurred_at for t in normal)
    ids = sorted({t.customer_id for t in normal})
    results: dict[str, object] = {"transfers": len(normal), "customers": len(ids)}

    whole = build_transaction_graph(normal)
    results["whole_year_graph"] = [whole.number_of_nodes(), whole.number_of_edges()]
    customer_set = set(ids)
    c2c = sum(1 for u, v in whole.edges if u in customer_set and v in customer_set)
    _out(f"Whole-year graph: {whole.number_of_nodes():,} nodes, {whole.number_of_edges():,} "
         f"edges, customer-to-customer edges: {c2c}")  # fmt: skip
    results["customer_to_customer_edges"] = c2c
    results["normal"] = _report_run("normal data", run_windows(normal, start, end))

    planter = Planter(ids, start, end, seed)
    positives = {planter.ring(3 + i % 3, (0.005, 0.02), (1, 36)) for i in range(rings)}
    negatives = {kind: [planter.negative(kind) for _ in range(10)] for kind in NEGATIVE_KINDS}
    planted: list[TransferLike] = [*normal, *planter.rows]
    run, peak = _measure(lambda: run_windows(planted, start, end))
    neg_hits = {kind: sum(s in run.alerts for s in sets) for kind, sets in negatives.items()}
    tp = len(positives & run.alerts)
    fp = len(run.alerts - positives)
    planted_report = _report_run("normal + planted", run)
    planted_report |= {
        "positives": len(positives),
        "detected": tp,
        "detection_rate": tp / len(positives),
        "false_positives": fp,
        "precision": tp / (tp + fp) if tp + fp else 1.0,
        "negatives_flagged": neg_hits,
        "tracemalloc_peak_mib": round(peak, 1),
    }
    results["planted"] = planted_report
    _out(f"  planted rings detected: {tp}/{len(positives)}, false positives: {fp}, "
         f"negatives flagged: {neg_hits}, tracemalloc peak {peak:.1f} MiB")  # fmt: skip

    stressed: list[TransferLike] = [*rewire(normal, ids, seed), *planter.rows]
    stress = run_windows(stressed, start, end)
    stress_report = _report_run("stress: rewired to customers", stress)
    cycles = run_windows(stressed, start, end, count_cycles=True).max_window_cycles
    stress_tp = len(positives & stress.alerts)
    stress_report |= {
        "detected": stress_tp,
        "false_positives": len(stress.alerts - positives),
        "max_cycles_len_le_5_per_window": cycles,
    }
    results["stress"] = stress_report
    _out(f"  planted detected: {stress_tp}/{len(positives)}, other alerts (false positives): "
         f"{len(stress.alerts - positives)}; busiest window holds {cycles:,} cycles of "
         f"length <= 5")  # fmt: skip
    results["process_max_rss_mib"] = _peak_rss_mib()
    _out(f"Process peak RSS: {results['process_max_rss_mib']} MiB")
    return results


def run_benchmark(seed: int) -> bool:
    """The hand-built benchmark: 4 planted rings and 9 distractor structures."""
    transfers, ground_truth = generate_circular_ring_evaluation_dataset(seed=seed)
    t0 = time.perf_counter()
    report = evaluate_circular_detection(transfers, ground_truth, lookback_window=LOOKBACK)
    elapsed = (time.perf_counter() - t0) * 1000
    boosts = calculate_risk_boosts(report.detected_loops)
    _out(f"Benchmark seed {seed}: {report.transfers_count} transfers, "
         f"{report.graph_nodes} nodes, {report.graph_edges} edges, {elapsed:.1f} ms")  # fmt: skip
    for loop in report.detected_loops:
        _out(f"  {loop.length}-ring, retention {loop.retention_ratio * 100:.1f}%, "
             f"{len(loop.transaction_ids)} transfers")  # fmt: skip
    _out(f"  TP {report.true_positives}  FP {report.false_positives}  "
         f"FN {report.false_negatives}  precision {report.precision:.0%}  "
         f"recall {report.recall:.0%}  accounts boosted {len(boosts)}")  # fmt: skip
    return report.precision == 1.0 and report.true_positives > 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    bench = sub.add_parser("benchmark")
    bench.add_argument("--seed", type=int, default=42)
    gen = sub.add_parser("generator")
    gen.add_argument("--seed", type=int, default=42)
    gen.add_argument("--customers", type=int, default=1000)
    gen.add_argument("--months", type=int, default=12)
    gen.add_argument("--rings", type=int, default=30)
    gen.add_argument("--json", default=None, help="also write the results to this file")
    args = parser.parse_args()
    if args.mode == "benchmark":
        return 0 if run_benchmark(args.seed) else 1
    results = run_generator(args.seed, args.customers, args.months, args.rings)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(results, handle, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
