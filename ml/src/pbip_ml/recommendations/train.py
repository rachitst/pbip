"""Train the rankers on the SIMULATED uptake panel and decide whether the API may use one.

    python -m pbip_datagen uptake --out data/recommendations
    python -m pbip_ml.recommendations.train --data data/recommendations --out models/recommendations

Writes ``report.json`` always: rules-v1, the logistic-regression baseline and the LightGBM ranker
on the validation and held-out test months, the gate verdict and the latency. Writes
``propensity_model.json`` and a SHA-256 ``manifest.json`` only if the gate promoted the model;
otherwise removes them, so ``propensity.load`` refuses the directory and the API keeps rules-v1.
Logs one MLflow run per model under ``pbip-recommendations``.
Every number in the report is measured on SIMULATED uptake, not on real customers.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import lightgbm as lgb
import mlflow

from pbip_ml.recommendations import training
from pbip_ml.recommendations.engine import MODEL_VERSION as RULES_VERSION
from pbip_ml.recommendations.panel import SUMMARY_FILE, Rows, build_rows, read_panel, time_split
from pbip_ml.recommendations.propensity import MANIFEST, MODEL_FILE, REPORT_FILE, PropensityModel
from pbip_ml.tracking import (
    DEFAULT_LOCAL_DIR,
    TrackingTarget,
    configure_tracking,
    ensure_experiment,
)

EXPERIMENT = "pbip-recommendations"
RANKER_FILE = "lightgbm_ranker.txt"  # for MLflow only; never served unless a later ADR says so


def _scores(train: Rows, rows: Rows, lr: PropensityModel, ranker: lgb.Booster) -> dict[str, Any]:
    return {
        RULES_VERSION: rows.rules_score,
        training.LR_VERSION: training.predict_logistic(lr, rows),
        training.LGBM_VERSION: training.predict_ranker(ranker, rows),
        training.POPULARITY: training.popularity(train, rows),
    }


def _sample(rows: Rows, limit: int) -> tuple[Any, ...]:
    """Every n-th test context, at most ``limit`` (deterministic)."""
    groups = sorted({int(g) for g in rows.group})
    step = max(len(groups) // limit, 1)
    return tuple(rows.contexts[g] for g in groups[::step][:limit])


def _evaluate(train: Rows, rows: Rows, lr: PropensityModel, ranker: lgb.Booster) -> dict[str, Any]:
    return {
        "rows": len(rows.label),
        "uptakes": int(rows.label.sum()),
        "customer_months": len(set(rows.group.tolist())),
        **training.compare(rows, _scores(train, rows, lr, ranker)),
    }


def run(data: Path, out: Path, sensitivity: Path | None = None) -> dict[str, Any]:
    """Train both models, evaluate, gate, write the artefacts; returns the report."""
    panel = read_panel(data)
    months = max(len(history) for history in panel.values())
    split = time_split(build_rows(panel), train_last=months // 2 - 1)
    lr = training.fit_logistic(split.train)
    ranker = training.fit_ranker(split.train, split.valid)
    validation = _evaluate(split.train, split.valid, lr, ranker)
    test = _evaluate(split.train, split.test, lr, ranker)
    candidate = training.choose_candidate(validation)
    ordering = lr if candidate == training.LR_VERSION else training.RankerOrdering(ranker)
    checks = training.engine_checks(ordering, _sample(split.test, training.ENGINE_CHECK_CONTEXTS))
    latency = training.measure_latency(lr, ranker, _sample(split.test, training.LATENCY_CONTEXTS))
    report: dict[str, Any] = {
        "data": "SIMULATED product uptake, not real customer behaviour",
        "simulation": json.loads((data / SUMMARY_FILE).read_text(encoding="utf-8")),
        "baseline": RULES_VERSION,
        "models": [training.LR_VERSION, training.LGBM_VERSION],
        "split": {
            "train_feature_months": f"<= {split.train_last}",
            "validation_feature_months": list(split.valid_months),
            "test_feature_months": f">= {split.test_first}",
            "train_rows": len(split.train.label),
            "train_uptakes": int(split.train.label.sum()),
        },
        "ranker": {"params": training.RANKER_PARAMS, "best_iteration": ranker.best_iteration},
        "validation": validation,
        "test": test,
        "engine_checks": checks,
        "latency": latency,
        "gate": training.gate(candidate, test, checks, latency),
    }
    if sensitivity is not None:
        rows = build_rows(read_panel(sensitivity))
        report["sensitivity"] = {
            "simulation": json.loads((sensitivity / SUMMARY_FILE).read_text(encoding="utf-8")),
            "test": _evaluate(split.train, time_split(rows, split.train_last).test, lr, ranker),
        }
    training.write_artifacts(lr, report, out)
    ranker.save_model(str(out / RANKER_FILE))
    return report


def log_to_mlflow(report: dict[str, Any], out: Path, target: TrackingTarget) -> dict[str, str]:
    """One run per model, each with its own and rules-v1's test metrics and the gate verdict."""
    experiment_id = ensure_experiment(EXPERIMENT, target)
    run_ids: dict[str, str] = {}
    for name in (training.LR_VERSION, training.LGBM_VERSION):
        with mlflow.start_run(experiment_id=experiment_id, run_name=name) as active:
            mlflow.log_params(
                {f"sim_{k}": v for k, v in report["simulation"].items() if k != "products"}
            )
            mlflow.log_param("model_version", name)
            mlflow.log_param("gate_candidate", report["gate"]["candidate"] == name)
            mlflow.log_param(
                "promoted", report["gate"]["promoted"] and report["gate"]["candidate"] == name
            )
            for split in ("validation", "test"):
                for scorer, prefix in ((name, "model"), (RULES_VERSION, "rules_v1")):
                    for key, value in report[split]["metrics"][scorer].items():
                        mlflow.log_metric(f"{split}_{prefix}_{key}", value)
            mlflow.log_metric("added_p95_ms", training.added_p95_ms(report["latency"], name))
            mlflow.log_artifact(str(out / REPORT_FILE))
            if name == training.LGBM_VERSION:
                mlflow.log_artifact(str(out / RANKER_FILE))
            else:
                for served in (MODEL_FILE, MANIFEST):
                    if (out / served).exists():
                        mlflow.log_artifact(str(out / served))
            run_ids[name] = str(active.info.run_id)
    return run_ids


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, required=True, help="pbip_datagen uptake export")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sensitivity", type=Path, default=None, help="a perturbed export")
    parser.add_argument("--mlflow-local-dir", type=Path, default=DEFAULT_LOCAL_DIR)
    args = parser.parse_args(argv)
    report = run(args.data, args.out, args.sensitivity)
    run_ids = log_to_mlflow(report, args.out, configure_tracking(local_dir=args.mlflow_local_dir))
    summary = {
        "mlflow_run_ids": run_ids,
        "gate": report["gate"],
        "test": report["test"]["metrics"],
        "differences": report["test"]["difference_vs_rules_v1"],
        "latency": report["latency"],
    }
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    main()
