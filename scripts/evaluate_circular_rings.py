"""Evaluate circular fund loop detection with NetworkX on synthetic transaction graphs.

Definition of Done for TASK-072:
A NetworkX script identifies synthetic circular rings with 100% precision.
"""

import argparse
import sys
import time
from datetime import timedelta
from typing import Any

from pbip_ml.fraud.circular import (
    DEFAULT_MAX_CYCLE_LENGTH,
    DEFAULT_MIN_CYCLE_LENGTH,
    DEFAULT_MIN_RETENTION,
    calculate_risk_boosts,
    evaluate_circular_detection,
    generate_circular_ring_evaluation_dataset,
)


def run_evaluation(
    seed: int = 42,
    lookback_days: int = 14,
    min_retention: float = DEFAULT_MIN_RETENTION,
    min_length: int = DEFAULT_MIN_CYCLE_LENGTH,
    max_length: int = DEFAULT_MAX_CYCLE_LENGTH,
) -> dict[str, Any]:
    """Execute synthetic circular ring evaluation and print comprehensive metrics."""
    sys.stdout.write("===============================================================\n")
    sys.stdout.write("PBIP TASK-072: NetworkX Circular Mule Ring Detection Evaluation\n")
    sys.stdout.write("===============================================================\n\n")

    t0 = time.perf_counter()
    transfers, ground_truth_rings = generate_circular_ring_evaluation_dataset(seed=seed)
    gen_time = (time.perf_counter() - t0) * 1000

    sys.stdout.write(f"1. Synthetic Dataset Generated in {gen_time:.2f} ms:\n")
    sys.stdout.write(f"   - Total Transfers: {len(transfers)}\n")
    sys.stdout.write(f"   - Ground Truth Rings: {len(ground_truth_rings)}\n")
    sys.stdout.write(f"   - Configured Lookback Window: {lookback_days} days\n")
    sys.stdout.write(f"   - Cycle Length Bounds: [{min_length}, {max_length}]\n")
    sys.stdout.write(f"   - Minimum Funds Retention Threshold: > {min_retention * 100:.1f}%\n\n")

    t1 = time.perf_counter()
    metrics = evaluate_circular_detection(
        transfers,
        ground_truth_rings,
        lookback_window=timedelta(days=lookback_days),
        min_length=min_length,
        max_length=max_length,
        min_retention=min_retention,
    )
    eval_time = (time.perf_counter() - t1) * 1000

    detected_loops = metrics["detected_loops"]
    risk_boosts = calculate_risk_boosts(detected_loops)

    sys.stdout.write(f"2. Graph Construction & Cycle Detection in {eval_time:.2f} ms:\n")
    sys.stdout.write(f"   - Directed Graph Nodes: {metrics['graph_nodes']}\n")
    sys.stdout.write(f"   - Directed Graph Edges: {metrics['graph_edges']}\n")
    sys.stdout.write(f"   - Total Detected Cycles: {metrics['detected_count']}\n\n")

    sys.stdout.write("3. Detected Circular Rings:\n")
    for idx, loop in enumerate(detected_loops, start=1):
        ret_pct = loop.retention_ratio * 100
        vol_paise = loop.bottleneck_minor
        vol_inr = vol_paise / 100
        sys.stdout.write(
            f"   [{idx}] Length {loop.length} Ring | Retention: {ret_pct:.1f}% | "
            f"Bottleneck: INR {vol_inr:,.2f} | Txns: {len(loop.transaction_ids)}\n"
        )
    sys.stdout.write("\n")

    sys.stdout.write("4. Risk Score Boosts Generated:\n")
    sys.stdout.write(f"   - Participating Accounts Boosted: {len(risk_boosts)}\n")
    for acct_id, boost in list(risk_boosts.items())[:5]:
        sys.stdout.write(
            f"   - Account {acct_id}: +{boost.risk_boost} pts "
            f"({boost.loop_count} loop(s), max retention {boost.highest_retention * 100:.1f}%)\n"
        )
    if len(risk_boosts) > 5:
        sys.stdout.write(f"   ... and {len(risk_boosts) - 5} more accounts.\n")
    sys.stdout.write("\n")

    tp = metrics["true_positives"]
    fp = metrics["false_positives"]
    fn = metrics["false_negatives"]
    precision = metrics["precision"]
    recall = metrics["recall"]

    sys.stdout.write("5. Ground-Truth Evaluation Metrics:\n")
    sys.stdout.write(f"   - True Positives (TP):  {tp}\n")
    sys.stdout.write(f"   - False Positives (FP): {fp}\n")
    sys.stdout.write(f"   - False Negatives (FN): {fn}\n")
    sys.stdout.write(f"   - Precision:            {precision * 100:.1f}%\n")
    sys.stdout.write(f"   - Recall:               {recall * 100:.1f}%\n\n")

    if precision == 1.0 and tp > 0:
        sys.stdout.write("SUCCESS: 100% Precision achieved against ground-truth synthetic rings!\n")
        sys.stdout.write("Definition of Done criteria satisfied.\n")
    else:
        sys.stdout.write("FAILURE: Precision did not meet 100% requirement.\n")

    sys.stdout.write("===============================================================\n")
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate NetworkX circular fund loop detection on synthetic data."
    )
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for evaluation data")
    parser.add_argument(
        "--lookback-days", type=int, default=14, help="Lookback window in days (default: 14)"
    )
    parser.add_argument(
        "--min-retention",
        type=float,
        default=DEFAULT_MIN_RETENTION,
        help="Minimum funds retention ratio (default: 0.90)",
    )
    parser.add_argument(
        "--min-length",
        type=int,
        default=DEFAULT_MIN_CYCLE_LENGTH,
        help="Minimum cycle length (default: 3)",
    )
    parser.add_argument(
        "--max-length",
        type=int,
        default=DEFAULT_MAX_CYCLE_LENGTH,
        help="Maximum cycle length (default: 5)",
    )
    args = parser.parse_args()

    metrics = run_evaluation(
        seed=args.seed,
        lookback_days=args.lookback_days,
        min_retention=args.min_retention,
        min_length=args.min_length,
        max_length=args.max_length,
    )

    if metrics["precision"] == 1.0 and metrics["true_positives"] > 0:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
