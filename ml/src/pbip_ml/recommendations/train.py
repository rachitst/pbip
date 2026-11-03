"""Train the simulated-uptake propensity model and decide whether the API may use it (ADR-0049).

    python -m pbip_ml.recommendations.train --out models/recommendations

Writes ``report.json`` always. Writes ``propensity_model.json`` and ``manifest.json`` only when the
model beats rules-v1 on the documented scenario's test months (``training.gate``); otherwise it
removes them, so the API keeps rules-v1. Logs the run to MLflow (``pbip-recommendations``).
Every number in the report is measured on SIMULATED uptake, not on real customers.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import mlflow

from pbip_ml.recommendations import training
from pbip_ml.recommendations.engine import MODEL_VERSION as RULES_VERSION
from pbip_ml.recommendations.propensity import (
    MANIFEST,
    MODEL_FILE,
    MODEL_VERSION,
    REPORT_FILE,
    SCORE_LABEL,
)
from pbip_ml.recommendations.simulation import (
    SIMULATION_VERSION,
    Scenario,
    UptakeAssumptions,
    perturbed,
    simulate,
)
from pbip_ml.tracking import (
    DEFAULT_LOCAL_DIR,
    TrackingTarget,
    configure_tracking,
    ensure_experiment,
)

EXPERIMENT = "pbip-recommendations"
DEFAULT_SEED = 42
HOLDOUT_SEED_OFFSET = 1000  # the held-out variants use other populations, never the training one


def _sensitivity(model: Any, seed: int, customers: int, months: int, cut: int) -> dict[str, Any]:
    """The trained model, unchanged, on two other simulated populations' test months."""
    variants = {
        "perturbed_effects_x0.7_base_plus_0.3": simulate(
            seed + HOLDOUT_SEED_OFFSET,
            customers,
            months,
            assumptions=perturbed(UptakeAssumptions(), 0.7, 0.3),
        ),
        "rules_follow (uptake follows rules-v1; rules-v1 should win here)": simulate(
            seed + HOLDOUT_SEED_OFFSET + 1, customers, months, scenario=Scenario.RULES_FOLLOW
        ),
    }
    return {
        name: training.evaluate(model, training.time_split(training.build_rows(snaps), cut).test)
        for name, snaps in variants.items()
    }


def run(
    seed: int, customers: int, months: int, holdout_customers: int, out: Path
) -> dict[str, Any]:
    cut = int(months * 0.6)
    rows = training.build_rows(simulate(seed, customers, months))
    split = training.time_split(rows, cut)
    model = training.fit(split.train)
    evaluation = training.evaluate(model, split.test)
    report: dict[str, Any] = {
        "model_version": MODEL_VERSION,
        "score_label": SCORE_LABEL,
        "baseline": RULES_VERSION,
        "data": "SIMULATED product uptake, not real customer behaviour",
        "simulation": {
            "version": SIMULATION_VERSION,
            "seed": seed,
            "customers": customers,
            "months": months,
            "scenario": Scenario.DOCUMENTED.value,
        },
        "split": {
            "train_feature_months": f"<= {split.train_last_feature_month}",
            "test_feature_months": f">= {split.test_first_feature_month}",
            "train_rows": len(split.train.label),
            "train_uptakes": int(split.train.label.sum()),
        },
        "test": evaluation,
        "gate": training.gate(evaluation),
        "sensitivity": _sensitivity(model, seed, holdout_customers, months, cut),
    }
    training.write_artifacts(model, report, out)
    return report


def log_to_mlflow(report: dict[str, Any], out: Path, target: TrackingTarget) -> str:
    experiment_id = ensure_experiment(EXPERIMENT, target)
    with mlflow.start_run(experiment_id=experiment_id) as active:
        mlflow.log_params({f"sim_{k}": v for k, v in report["simulation"].items()})
        mlflow.log_param("model_version", MODEL_VERSION)
        mlflow.log_param("promoted", report["gate"]["promoted"])
        for scorer in (MODEL_VERSION, RULES_VERSION):
            section = report["test"][scorer]
            prefix = "model" if scorer == MODEL_VERSION else "rules_v1"
            mlflow.log_metric(f"{prefix}_macro_pr_auc", section["macro_pr_auc"])
            for key, value in section["ranking"].items():
                mlflow.log_metric(f"{prefix}_{key}", value)
        for name in (REPORT_FILE, MODEL_FILE, MANIFEST):
            if (out / name).exists():
                mlflow.log_artifact(str(out / name))
        return str(active.info.run_id)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--customers", type=int, default=3000)
    parser.add_argument("--holdout-customers", type=int, default=1000)
    parser.add_argument("--months", type=int, default=24)
    parser.add_argument("--mlflow-local-dir", type=Path, default=DEFAULT_LOCAL_DIR)
    args = parser.parse_args(argv)
    report = run(args.seed, args.customers, args.months, args.holdout_customers, args.out)
    run_id = log_to_mlflow(report, args.out, configure_tracking(local_dir=args.mlflow_local_dir))
    summary = {
        "mlflow_run_id": run_id,
        "promoted": report["gate"]["promoted"],
        "gate": report["gate"]["checks"],
        MODEL_VERSION: {k: report["test"][MODEL_VERSION][k] for k in ("macro_pr_auc", "ranking")},
        RULES_VERSION: {k: report["test"][RULES_VERSION][k] for k in ("macro_pr_auc", "ranking")},
    }
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    main()
