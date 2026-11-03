"""Bounded LightGBM hyper-parameter search for the fraud model (TASK-084). Dev tool, not shipped.

Usage (the export ``pbip_ml.fraud.train`` trains on)::

    uv run python scripts/fraud_tune.py --data data/fraud --trials 30 --json tune.json

The search never sees the test period. It uses the training and validation periods of the same
time split ``train`` uses, cut into expanding-window time folds: fit on the earlier blocks, stop
early on the first half of the next block, score PR-AUC on its second half (classes are
imbalanced, so never accuracy). The objective is the mean PR-AUC over the folds. Optuna's TPE
sampler has a fixed seed and the current ``LGBM_PARAMS`` are trial 0, so today's model is compared
on exactly the same folds.

The winner is only a candidate. It replaces ``LGBM_PARAMS`` only if the full pipeline with it
(``python -m pbip_ml.fraud.train --lgbm-params``) keeps the four hybrid bars and raises the served
PR-AUC on the test period AND on the held-out seed (``docs/ml/model-cards/lightgbm-fraud.md``).
"""

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import optuna
import pandas as pd
from pbip_ml.fraud import training as t
from pbip_ml.fraud.train import load_export
from sklearn.metrics import average_precision_score

SEED = 42
TUNED = ("learning_rate", "max_depth", "num_leaves", "min_child_samples", "lambda_l1",
         "lambda_l2", "feature_fraction")  # fmt: skip


def time_folds(frame: pd.DataFrame, blocks: int = 5) -> list[tuple[pd.DataFrame, pd.DataFrame]]:
    """Expanding window: fold k fits on blocks 0..k and evaluates on block k+1 (k >= 1)."""
    ordered = frame.sort_values("occurred_at", kind="stable")
    cuts = [ordered["occurred_at"].quantile(i / blocks) for i in range(1, blocks)]
    block = np.searchsorted(np.asarray(cuts), ordered["occurred_at"].to_numpy(), side="right")
    return [(ordered[block <= k], ordered[block == k + 1]) for k in range(1, blocks - 1)]


def fold_pr_auc(params: dict[str, Any], fit: pd.DataFrame, held: pd.DataFrame) -> float:
    """Early stopping on the first half of ``held`` (by time), PR-AUC on the second half."""
    half = held["occurred_at"].quantile(0.5)
    stop, score = held[held["occurred_at"] < half], held[held["occurred_at"] >= half]
    booster, _ = t.train_lightgbm(t.Split(fit, stop, score), params=params)
    x, y = t._xy(score)
    predicted = np.asarray(booster.predict(x, num_iteration=booster.best_iteration))
    return float(average_precision_score(y, predicted)) if y.any() else 0.0


def suggest(trial: optuna.Trial) -> dict[str, Any]:
    """Learning rate, depth and regularisation (TASK-084); num_leaves capped by the depth."""
    depth = trial.suggest_int("max_depth", 3, 10)
    return {
        "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
        "max_depth": depth,
        "num_leaves": trial.suggest_int("num_leaves", 7, min(63, 2**depth - 1)),
        "min_child_samples": trial.suggest_int("min_child_samples", 5, 100, log=True),
        "lambda_l1": trial.suggest_float("lambda_l1", 1e-3, 10.0, log=True),
        "lambda_l2": trial.suggest_float("lambda_l2", 1e-3, 10.0, log=True),
        "feature_fraction": trial.suggest_float("feature_fraction", 0.6, 1.0),
    }


def current_params() -> dict[str, Any]:
    """Today's ``LGBM_PARAMS`` in the search space (LightGBM's defaults where it sets none)."""
    defaults = {"max_depth": 10, "lambda_l1": 1e-3, "lambda_l2": 1e-3}  # ~unbounded, ~no penalty
    return {name: t.LGBM_PARAMS.get(name, defaults.get(name)) for name in TUNED}


def search(
    frame: pd.DataFrame, trials: int, blocks: int = 5, timeout: float | None = None
) -> dict[str, Any]:
    """At most ``trials`` trials; ``timeout`` (seconds) stops early on a shared machine."""
    folds = time_folds(frame, blocks)
    as_is = float(np.mean([fold_pr_auc(t.LGBM_PARAMS, a, b) for a, b in folds]))

    def objective(trial: optuna.Trial) -> float:
        params = {**t.LGBM_PARAMS, **suggest(trial)}
        return float(np.mean([fold_pr_auc(params, a, b) for a, b in folds]))

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=SEED))
    study.enqueue_trial(current_params())
    study.optimize(objective, n_trials=trials, timeout=timeout)
    return {
        "trials": len(study.trials),
        "folds": [
            {"fit_rows": len(a), "eval_rows": len(b), "eval_positives": int(b["label"].sum())}
            for a, b in folds
        ],
        "current_params_pr_auc": round(as_is, 4),
        "best_pr_auc": round(float(study.best_value), 4),
        "best_params": study.best_params,
        "all": [
            {"number": tr.number, "value": round(float(tr.value or 0.0), 4), **tr.params}
            for tr in study.trials
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=None, help="seconds; stop starting trials")
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--best-params", type=Path, default=None,
                        help="also write the winner as a --lgbm-params file for train")  # fmt: skip
    args = parser.parse_args()
    split = t.time_split(load_export(args.data))
    result = search(pd.concat([split.train, split.validation]), args.trials, timeout=args.timeout)
    args.json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if args.best_params is not None:
        args.best_params.write_text(json.dumps(result["best_params"], indent=2) + "\n", "utf-8")


if __name__ == "__main__":
    main()
