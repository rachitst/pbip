"""Numbers and charts for docs/ml/model-evaluation-report-v1.md (TASK-039). Dev tool, not shipped.

Usage (after ``python -m pbip_ml.fraud.train --data D --holdout H --out M``)::

    uv run python scripts/fraud_eval_report.py --model M --data D --holdout H \
        --json eval.json --img docs/ml/img

Uses the served inference module (``pbip_ml.fraud.inference.load``) so the evaluated scorer is the
one the API loads. Latency is wall time on the machine that runs it: name the machine in the report.
"""

import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from pbip_ml.fraud import hybrid as h
from pbip_ml.fraud import training as t
from pbip_ml.fraud.evaluation import cheapest, cost_curve
from pbip_ml.fraud.features import PastTransfer
from pbip_ml.fraud.inference import TransferInputs, load, score_transfer
from pbip_ml.fraud.train import load_export
from sklearn.metrics import precision_recall_curve

REVIEW_COSTS_INR = (50, 150, 500)  # analyst cost per flagged transfer; 150 = ~10 min at 900/h


def _amounts(frame: Any) -> np.ndarray:
    return np.rint(np.power(10.0, frame["log_amount"].to_numpy())).astype(np.int64)


def _confusions(y: np.ndarray, risk: np.ndarray) -> dict[str, dict[str, float | int]]:
    return {
        name: t.evaluate(y, risk / 100, cut / 100).as_dict()
        for name, cut in (("step_up", h.STEP_UP_AT), ("review", h.REVIEW_AT), ("block", h.BLOCK_AT))
    }


def _first_time_fpr(frame: Any, risk: np.ndarray) -> dict[str, dict[str, float | int]]:
    """False-positive rates on LEGITIMATE first-time behaviour (#251), at STEP_UP and REVIEW."""
    normal = (frame["label"].to_numpy() == 0) & (frame["is_debit"].to_numpy() == 1)
    payee_age = frame["payee_age_seconds"].to_numpy()
    groups = {
        "new_payee_under_3_days": normal & (payee_age >= 0) & (payee_age < 3 * 86_400),
        "new_device": normal & (frame["new_device"].to_numpy() == 1),
        "failed_logins": normal & (frame["failure_count_24h"].to_numpy() > 0),
        "all_legitimate_debits": normal,
    }
    return {
        name: {
            "rows": int(mask.sum()),
            "fpr_at_step_up": round(float((risk[mask] >= h.STEP_UP_AT).mean()), 4)
            if mask.any()
            else 0,
            "fpr_at_review": round(float((risk[mask] >= h.REVIEW_AT).mean()), 4)
            if mask.any()
            else 0,
        }
        for name, mask in groups.items()
    }


def _scenario_of_rows(directory: Path) -> np.ndarray:
    """Scenario of each fraud row of an export, "" for legitimate rows (``load_export`` order)."""
    ids = pd.read_csv(directory / "transfers.csv", usecols=["id"])["id"]
    labels = pd.read_csv(directory / "fraud_labels.csv")
    fraud = labels[labels["fraudulent"].astype(str) == "True"]
    scenario = dict(zip(fraud["transaction_id"], fraud["scenario"], strict=True))
    of_rows: np.ndarray = ids.map(scenario).fillna("").to_numpy(dtype=str)
    return of_rows


def _recall_by_scenario(frame: Any, risk: np.ndarray, scenarios: np.ndarray) -> dict[str, Any]:
    """Recall per scenario at STEP_UP and REVIEW, and over F1-F6 only (comparable to v2.1)."""
    of_rows = scenarios[frame.index.to_numpy()]
    groups = {s: of_rows == s for s in sorted(set(of_rows) - {""})}
    groups["F1-F6"] = np.isin(of_rows, ["F1", "F2", "F3", "F4", "F5", "F6"])
    return {
        name: {
            "rows": int(mask.sum()),
            "recall_at_step_up": round(float((risk[mask] >= h.STEP_UP_AT).mean()), 4),
            "recall_at_review": round(float((risk[mask] >= h.REVIEW_AT).mean()), 4),
        }
        for name, mask in groups.items()
        if mask.any()
    }


def _latency(scorer: h.HybridScorer) -> dict[str, Any]:
    now = datetime(2026, 6, 15, 13, 0, tzinfo=UTC)
    history = [PastTransfer(now - timedelta(days=i), "DEBIT", 2_000_00, None) for i in range(1, 30)]
    case = TransferInputs(1_800_00, "INR", now, now - timedelta(days=200), None, False, 0, history)
    for _ in range(20):
        score_transfer(scorer, case)  # warm-up, as the API loader does

    def timed(_: int) -> float:
        started = time.perf_counter()
        score_transfer(scorer, case)
        return (time.perf_counter() - started) * 1000

    out: dict[str, Any] = {}
    for workers in (1, 2, 4):
        calls = 400
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            timings = sorted(pool.map(timed, range(calls)))
        elapsed = time.perf_counter() - started
        out[f"threads_{workers}"] = {
            "calls": calls,
            "p50_ms": round(statistics.median(timings), 2),
            "p95_ms": round(timings[int(calls * 0.95) - 1], 2),
            "max_ms": round(timings[-1], 2),
            "throughput_per_s": round(calls / elapsed, 1),
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    for name in ("--model", "--data", "--holdout", "--json", "--img"):
        parser.add_argument(name, type=Path, required=True)
    parser.add_argument("--skip-latency", action="store_true", help="metrics only (faster)")
    args = parser.parse_args()
    args.img.mkdir(parents=True, exist_ok=True)

    scorer = load(args.model)
    rules_only = h.HybridScorer(scorer.params, None, None, scorer.rules)
    split = t.time_split(load_export(args.data))
    held = load_export(args.holdout)
    test, y = split.test, split.test["label"].to_numpy()
    assert scorer.booster is not None
    assert scorer.anomaly is not None
    scores = {
        "Hybrid (served)": h.batch_risk(scorer, test) / 100,
        "LightGBM alone": np.asarray(scorer.booster.predict(test[list(t.MODEL_FEATURES)])),
        "Isolation Forest alone": scorer.anomaly.score(test),
        "Rules only (fail-safe)": h.batch_risk(rules_only, test) / 100,
    }
    report: dict[str, Any] = {"test": {}, "holdout": {}, "cost": {}}
    fig, ax = plt.subplots(figsize=(6.4, 4.4))
    for label, score in scores.items():
        precision, recall, _ = precision_recall_curve(y, score)
        ax.plot(recall, precision, label=label)
    ax.set(xlabel="Recall", ylabel="Precision", title="Precision-recall, time-split test period")
    ax.legend(loc="lower left")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(args.img / "fraud-pr-curves.png", dpi=120)

    risk_test = h.batch_risk(scorer, test)
    risk_held = h.batch_risk(scorer, held)
    report["test"]["hybrid"] = _confusions(y, risk_test)
    report["test"]["rules_only"] = _confusions(y, h.batch_risk(rules_only, test))
    report["holdout"]["hybrid"] = _confusions(held["label"].to_numpy(), risk_held)
    report["test"]["first_time_fpr"] = _first_time_fpr(test, risk_test)
    report["holdout"]["first_time_fpr"] = _first_time_fpr(held, risk_held)
    rules_test = h.batch_risk(rules_only, test)
    for name, frame, risk, directory in (
        ("test", test, risk_test, args.data),
        ("holdout", held, risk_held, args.holdout),
    ):
        report[name]["recall_by_scenario"] = _recall_by_scenario(
            frame, risk, _scenario_of_rows(directory)
        )
    report["test"]["rules_only_recall_by_scenario"] = _recall_by_scenario(
        test, rules_test, _scenario_of_rows(args.data)
    )

    amounts = _amounts(test)
    report["test"]["fraud_amount_minor"] = int(amounts[y == 1].sum())
    fig, ax = plt.subplots(figsize=(6.4, 4.4))
    for inr in REVIEW_COSTS_INR:
        points = cost_curve(y, risk_test, amounts, inr * 100)
        best = cheapest(points)
        at_review = points[h.REVIEW_AT]
        report["cost"][f"review_cost_inr_{inr}"] = {
            "best_threshold": best.threshold,
            "best_total_inr": best.total_minor // 100,
            "best_flagged": best.flagged,
            "best_missed_fraud": best.missed_fraud,
            "at_review_threshold_total_inr": at_review.total_minor // 100,
            "at_review_flagged": at_review.flagged,
            "at_review_missed": at_review.missed_fraud,
            "flag_nothing_total_inr": points[100].total_minor // 100,
        }
        ax.plot(
            [p.threshold for p in points],
            [p.total_minor / 100_00_000 for p in points],  # in lakh
            label=f"review cost INR {inr}",
        )
    ax.axvline(h.REVIEW_AT, color="grey", linestyle="--", linewidth=1)
    ax.set(xlabel="Risk threshold (flag when risk >= threshold)", ylabel="Expected cost (INR lakh)",
           title="Cost of reviews + missed fraud, test period")  # fmt: skip
    ax.set_yscale("log")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(args.img / "fraud-cost-curve.png", dpi=120)

    if not args.skip_latency:
        report["latency"] = _latency(scorer)
    args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
