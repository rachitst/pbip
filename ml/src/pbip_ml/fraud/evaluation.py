"""Offline evaluation of the served fraud scorer: cost curve (TASK-039) and the numbers the
promotion gate (TASK-038) decides on. Offline only.

A retail bank does not pick a threshold for F1; it picks the one that costs least. Every flagged
transfer costs an analyst review; every missed fraud costs the money that left. The costs are
INPUTS (stated in the report), not facts: change them and the best threshold moves.

``served_metrics`` and ``latency_profile`` are shared by ``scripts/fraud_eval_report.py`` and
``pbip_ml.fraud.promotion`` so the promotion gate measures a model exactly as the evaluation report
does: the served hybrid (``batch_risk``, same arithmetic as ``score``) and ``score_transfer`` timed
one transfer at a time, never numbers a training run reported about itself.
"""

import statistics
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from pbip_ml.fraud import hybrid as h
from pbip_ml.fraud import training as t
from pbip_ml.fraud.features import PastTransfer
from pbip_ml.fraud.inference import TransferInputs, score_transfer


@dataclass(frozen=True)
class CostPoint:
    threshold: int
    flagged: int
    missed_fraud: int
    review_cost_minor: int
    fraud_loss_minor: int

    @property
    def total_minor(self) -> int:
        return self.review_cost_minor + self.fraud_loss_minor


def cost_curve(
    labels: np.ndarray, risk: np.ndarray, amounts_minor: np.ndarray, review_cost_minor: int
) -> list[CostPoint]:
    """Expected cost at every integer threshold 0..100 (flag when ``risk >= threshold``).

    Flagged transfers each cost ``review_cost_minor``. A fraud below the threshold loses its full
    amount (no recovery assumed: the conservative case for push payments).
    """
    points = []
    for threshold in range(101):
        flagged = risk >= threshold
        missed = (labels == 1) & ~flagged
        points.append(
            CostPoint(
                threshold=threshold,
                flagged=int(flagged.sum()),
                missed_fraud=int(missed.sum()),
                review_cost_minor=int(flagged.sum()) * review_cost_minor,
                fraud_loss_minor=int(amounts_minor[missed].sum()),
            )
        )
    return points


def cheapest(points: list[CostPoint]) -> CostPoint:
    """Lowest total cost; ties go to the higher threshold (fewer customers interrupted)."""
    return min(points, key=lambda p: (p.total_minor, -p.threshold))


def served_metrics(scorer: h.HybridScorer, frame: pd.DataFrame) -> t.Metrics:
    """The served hybrid on a labelled frame, flagged at REVIEW (report section 3.1).

    PR-AUC is threshold-free (computed on the risk score); precision/recall/FPR are at REVIEW.
    """
    risk = h.batch_risk(scorer, frame)
    return t.evaluate(frame["label"].to_numpy(), risk / 100, h.REVIEW_AT / 100)


def _latency_case() -> TransferInputs:
    now = datetime(2026, 6, 15, 13, 0, tzinfo=UTC)
    history = [PastTransfer(now - timedelta(days=i), "DEBIT", 2_000_00, None) for i in range(1, 30)]
    return TransferInputs(1_800_00, "INR", now, now - timedelta(days=200), None, False, 0, history)


def latency_profile(
    scorer: h.HybridScorer, workers: Sequence[int] = (1, 2, 4), calls: int = 400
) -> dict[str, dict[str, float | int]]:
    """Wall-clock time of ``score_transfer`` (one transfer per call) after a warm-up, as the API
    loader warms up. ``threads_1`` is the single-request latency; more threads show queueing."""
    case = _latency_case()
    for _ in range(20):
        score_transfer(scorer, case)

    def timed(_: int) -> float:
        started = time.perf_counter()
        score_transfer(scorer, case)
        return (time.perf_counter() - started) * 1000

    out: dict[str, dict[str, float | int]] = {}
    for count in workers:
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=count) as pool:
            timings = sorted(pool.map(timed, range(calls)))
        elapsed = time.perf_counter() - started
        out[f"threads_{count}"] = {
            "calls": calls,
            "p50_ms": round(statistics.median(timings), 2),
            "p95_ms": round(timings[max(int(calls * 0.95) - 1, 0)], 2),
            "max_ms": round(timings[-1], 2),
            "throughput_per_s": round(calls / elapsed, 1),
        }
    return out
