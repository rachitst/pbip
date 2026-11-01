"""Train and evaluate the fraud models from exported labelled data (TASK-029, TASK-030).

Usage::

    uv run python -m pbip_datagen export --seed 42 --customers 3000 --months 6 --out data/fraud
    uv run python -m pbip_datagen export --seed 4242 --customers 1500 --months 6 --out data/holdout
    uv run python -m pbip_ml.fraud.train --data data/fraud --holdout data/holdout --out models/fraud

Writes ``report.json`` (metrics on the time-split test set and on the held-out seed, latencies),
``lightgbm.txt``, ``isolation_forest.joblib``, ``hybrid_params.json`` and ``manifest.json``
(SHA-256 of each, checked by the API loader), and logs the run (params, metrics, artifacts)
to MLflow - the server if reachable, otherwise the local fallback store (``pbip_ml.tracking``).
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import joblib
import mlflow
import numpy as np
import pandas as pd

from pbip_ml.fraud import hybrid as h
from pbip_ml.fraud import training as t
from pbip_ml.fraud.contract import FraudDecision
from pbip_ml.fraud.inference import write_manifest
from pbip_ml.fraud.rules import default_registry
from pbip_ml.tracking import (
    DEFAULT_LOCAL_DIR,
    TrackingTarget,
    configure_tracking,
    ensure_experiment,
)

EXPERIMENT = "pbip-fraud"
HYBRID_VERSION = "hybrid-lgbm-iforest-1"


def load_export(directory: Path) -> pd.DataFrame:
    """``transfers.csv`` + ``fraud_labels.csv`` from ``python -m pbip_datagen export``."""
    transfers = pd.read_csv(directory / "transfers.csv", keep_default_na=False)
    transfers["occurred_at"] = pd.to_datetime(transfers["occurred_at"], format="ISO8601", utc=True)
    transfers["payee_added_at"] = pd.to_datetime(
        transfers["payee_added_at"].replace("", None), format="ISO8601", utc=True
    )
    transfers["new_device"] = transfers["new_device"].astype(str) == "True"
    labels = pd.read_csv(directory / "fraud_labels.csv")
    labels["fraudulent"] = labels["fraudulent"].astype(str) == "True"
    return t.build_dataset(transfers, labels)


def run(data: Path, out: Path, holdout: Path | None = None) -> dict[str, Any]:
    dataset = load_export(data)
    split = t.time_split(dataset)
    booster, threshold = t.train_lightgbm(split)
    anomaly = t.train_isolation_forest(split)

    x_test, y_test = t._xy(split.test)
    lgbm_scores = np.asarray(booster.predict(x_test, num_iteration=booster.best_iteration))
    report: dict[str, Any] = {
        "rows": {"train": len(split.train), "validation": len(split.validation),
                 "test": len(split.test)},
        "time_ranges": {
            name: [str(part["occurred_at"].min()), str(part["occurred_at"].max())]
            for name, part in (("train", split.train), ("validation", split.validation),
                               ("test", split.test))
        },
        "lightgbm": {
            "best_iteration": booster.best_iteration,
            "test": t.evaluate(y_test, lgbm_scores, threshold).as_dict(),
            "latency": t.single_prediction_ms(
                lambda row: np.asarray(booster.predict(row, num_iteration=booster.best_iteration)),
                x_test.iloc[[0]],
            ),
        },
        "isolation_forest": {
            "test_at_0_5": t.evaluate(y_test, anomaly.score(split.test), 0.5).as_dict(),
            "latency": t.single_prediction_ms(anomaly.score, split.test.iloc[[0]]),
        },
    }  # fmt: skip
    if holdout is not None:
        held = load_export(holdout)
        x_held, y_held = t._xy(held)
        report["lightgbm"]["holdout_seed"] = t.evaluate(
            y_held,
            np.asarray(booster.predict(x_held, num_iteration=booster.best_iteration)),
            threshold,
        ).as_dict()

    rules = default_registry()
    params = h.fit_params(split.validation, booster, anomaly, rules, HYBRID_VERSION)
    full = h.HybridScorer(params, booster, anomaly, rules)
    rules_only = h.HybridScorer(params, None, None, rules)
    for name, scorer in (("hybrid", full), ("rules_only", rules_only)):
        risk_int = h.batch_risk(scorer, split.test)
        risk = risk_int / 100
        report[name] = {
            "decisions": {
                d.value: int(sum(h.decide(int(r)) is d for r in risk_int)) for d in FraudDecision
            },
            "at_step_up": t.evaluate(y_test, risk, h.STEP_UP_AT / 100).as_dict(),
            "at_review": t.evaluate(y_test, risk, h.REVIEW_AT / 100).as_dict(),
        }
    report["hybrid"]["params"] = json.loads(params.to_json()) | {
        "calibration_x": f"{len(params.calibration_x)} points",
        "calibration_y": f"{len(params.calibration_y)} points",
    }

    out.mkdir(parents=True, exist_ok=True)
    (out / "hybrid_params.json").write_text(params.to_json() + "\n", encoding="utf-8")
    booster.save_model(str(out / "lightgbm.txt"), num_iteration=booster.best_iteration)
    joblib.dump(anomaly, out / "isolation_forest.joblib")
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    write_manifest(out)  # SHA-256 per artifact: the API refuses files that do not match
    return report


def log_to_mlflow(
    report: dict[str, Any],
    out: Path,
    local_dir: Path,
    target: TrackingTarget | None = None,
) -> str:
    """Log the run. ``target`` pins the store so tests never fall through to a live server."""
    if target is None:
        target = configure_tracking(local_dir=local_dir)
    else:
        mlflow.set_tracking_uri(target.uri)
    experiment_id = ensure_experiment(EXPERIMENT, target)
    with mlflow.start_run(experiment_id=experiment_id) as active:
        mlflow.log_params({k: v for k, v in t.LGBM_PARAMS.items() if k != "verbose"})
        mlflow.log_param("features", ",".join(t.MODEL_FEATURES))
        for model, section in (("lgbm", "test"), ("if", "test_at_0_5")):
            source = report["lightgbm" if model == "lgbm" else "isolation_forest"][section]
            for key in ("precision", "recall", "f1", "pr_auc", "fpr", "fnr"):
                mlflow.log_metric(f"{model}_{key}", float(source[key]))
        for name in (
            "report.json",
            "lightgbm.txt",
            "isolation_forest.joblib",
            "hybrid_params.json",
            "manifest.json",
        ):
            mlflow.log_artifact(str(out / name))
        return str(active.info.run_id)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--holdout", type=Path, default=None)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mlflow-local-dir", type=Path, default=DEFAULT_LOCAL_DIR)
    args = parser.parse_args(argv)
    report = run(args.data, args.out, args.holdout)
    run_id = log_to_mlflow(report, args.out, args.mlflow_local_dir)
    sys.stdout.write(json.dumps({"mlflow_run_id": run_id, **report}, indent=2) + "\n")


if __name__ == "__main__":
    main()
