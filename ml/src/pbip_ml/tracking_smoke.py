"""Log a dummy experiment run to verify the MLflow integration end to end.

    uv run python -m pbip_ml.tracking_smoke            # server if reachable, else local fallback
    uv run python -m pbip_ml.tracking_smoke --require-server   # fail instead of falling back

The run records parameters, a metric series, tags and a JSON artifact, then reads them back.
The values are placeholders that exercise the plumbing; they describe no model.
"""

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import mlflow
from mlflow.tracking import MlflowClient

from pbip_ml.tracking import (
    DEFAULT_LOCAL_DIR,
    TrackingTarget,
    configure_tracking,
    ensure_experiment,
)

EXPERIMENT = "pbip-smoke"


@dataclass(frozen=True, slots=True)
class SmokeResult:
    target: TrackingTarget
    run_id: str
    artifacts: tuple[str, ...]


def log_dummy_run(target: TrackingTarget) -> SmokeResult:
    experiment_id = ensure_experiment(EXPERIMENT, target)
    with mlflow.start_run(experiment_id=experiment_id, run_name="integration-smoke") as run:
        mlflow.log_params({"model": "dummy-baseline", "seed": 42, "feature_set": "v0.1-draft"})
        mlflow.set_tags({"purpose": "integration-smoke", "task": "TASK-011"})
        for step, value in enumerate((0.50, 0.62, 0.71)):
            mlflow.log_metric("pr_auc", value, step=step)
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "summary.json"
            summary.write_text(json.dumps({"note": "smoke run, not a model"}), encoding="utf-8")
            mlflow.log_artifact(str(summary))
    client = MlflowClient(tracking_uri=target.uri)
    stored = client.get_run(run.info.run_id)
    if stored.data.params.get("model") != "dummy-baseline" or "pr_auc" not in stored.data.metrics:
        raise RuntimeError("run was not persisted as logged")
    artifacts = tuple(a.path for a in client.list_artifacts(run.info.run_id))
    return SmokeResult(target, run.info.run_id, artifacts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracking-uri", default=None)
    parser.add_argument("--local-dir", type=Path, default=DEFAULT_LOCAL_DIR)
    parser.add_argument("--require-server", action="store_true")
    args = parser.parse_args(argv)
    target = configure_tracking(args.tracking_uri, args.local_dir)
    if args.require_server and not target.is_remote:
        sys.stderr.write(
            "error: MLflow server not reachable (start: docker compose --profile ml up)\n"
        )
        return 1
    result = log_dummy_run(target)
    where = "server" if target.is_remote else "local fallback"
    artifacts = list(result.artifacts)
    sys.stdout.write(f"[OK] logged run {result.run_id} to {where} {target.uri}; {artifacts=}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
