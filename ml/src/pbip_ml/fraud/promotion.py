"""Automated MLflow model registry promotion workflow (TASK-038).

Evaluates candidate fraud models against a baseline metrics gate (PR-AUC, latency) and against
the incumbent champion model. Winning models are registered in the MLflow Model Registry and
promoted by tagging them with the alias 'champion' (with previous champions tagged as 'challenger').
"""

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mlflow.artifacts import download_artifacts
from mlflow.entities import Run
from mlflow.tracking import MlflowClient

from pbip_ml.tracking import DEFAULT_LOCAL_DIR, resolve_tracking_target

_logger = logging.getLogger(__name__)

DEFAULT_REGISTERED_MODEL_NAME = "pbip-fraud"
DEFAULT_CHAMPION_ALIAS = "champion"
DEFAULT_CHALLENGER_ALIAS = "challenger"
DEFAULT_MIN_PR_AUC = 0.85
DEFAULT_MAX_LATENCY_MS = 25.0


@dataclass(frozen=True, slots=True)
class MetricsGate:
    """Thresholds required for a candidate model to qualify as a champion."""

    min_pr_auc: float = DEFAULT_MIN_PR_AUC
    max_latency_ms: float = DEFAULT_MAX_LATENCY_MS
    require_better_or_equal_pr_auc: bool = True
    max_latency_regression_pct: float = 20.0


DEFAULT_METRICS_GATE = MetricsGate()


@dataclass(frozen=True, slots=True)
class ModelMetrics:
    """Metrics extracted from a candidate or champion model run."""

    pr_auc: float
    latency_ms: float
    precision: float | None = None
    recall: float | None = None
    f1: float | None = None
    model_version: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """Outcome of evaluating a candidate model against the gate and champion."""

    passed: bool
    candidate_metrics: ModelMetrics
    champion_metrics: ModelMetrics | None
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "candidate_metrics": self.candidate_metrics.as_dict(),
            "champion_metrics": self.champion_metrics.as_dict() if self.champion_metrics else None,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class PromotionResult:
    """Outcome of the model promotion workflow."""

    promoted: bool
    candidate_version: str | None
    candidate_run_id: str
    registered_model_name: str
    alias: str
    evaluation: EvaluationResult
    exported_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "promoted": self.promoted,
            "candidate_version": self.candidate_version,
            "candidate_run_id": self.candidate_run_id,
            "registered_model_name": self.registered_model_name,
            "alias": self.alias,
            "evaluation": self.evaluation.as_dict(),
            "exported_path": self.exported_path,
        }


def _extract_from_report(run_id: str, client: MlflowClient) -> dict[str, Any]:
    """Read metrics from report.json if present in the run's artifacts."""
    try:
        downloaded = download_artifacts(
            run_id=run_id,
            artifact_path="report.json",
            tracking_uri=client.tracking_uri,
        )
        report_path = Path(downloaded)
        if not report_path.is_file():
            return {}
        data = json.loads(report_path.read_text(encoding="utf-8"))
        lgbm_test = data.get("lightgbm", {}).get("test", {})
        lgbm_lat = data.get("lightgbm", {}).get("latency", {})
        hybrid_p = data.get("hybrid", {}).get("params", {})
        return {
            "pr_auc": lgbm_test.get("pr_auc"),
            "precision": lgbm_test.get("precision"),
            "recall": lgbm_test.get("recall"),
            "f1": lgbm_test.get("f1"),
            "latency_ms": lgbm_lat.get("p95_ms"),
            "model_version": hybrid_p.get("model_version"),
        }
    except Exception as exc:
        _logger.debug("Failed reading report.json for run %s: %s", run_id, exc)
        return {}


def _extract_from_run_metrics(run: Run) -> dict[str, Any]:
    """Read metrics from run.data.metrics and params."""
    m = run.data.metrics
    pr_auc = m.get("lgbm_pr_auc", m.get("pr_auc", 0.0))
    latency = m.get("latency_p95_ms", m.get("lgbm_latency_p95_ms", m.get("latency_ms", 1.0)))
    mv = run.data.params.get("model_version", run.data.tags.get("model_version"))
    return {
        "pr_auc": float(pr_auc),
        "latency_ms": float(latency),
        "precision": float(m["lgbm_precision"]) if "lgbm_precision" in m else None,
        "recall": float(m["lgbm_recall"]) if "lgbm_recall" in m else None,
        "f1": float(m["lgbm_f1"]) if "lgbm_f1" in m else None,
        "model_version": mv,
    }


def extract_metrics(run: Run, client: MlflowClient | None = None) -> ModelMetrics:
    """Extract PR-AUC and latency metrics from a run's report artifact or logged metrics."""
    report_vals = _extract_from_report(run.info.run_id, client) if client else {}
    fallback_vals = _extract_from_run_metrics(run)

    pr_auc = report_vals.get("pr_auc") or fallback_vals["pr_auc"]
    latency_ms = report_vals.get("latency_ms") or fallback_vals["latency_ms"]
    precision = report_vals.get("precision", fallback_vals["precision"])
    recall = report_vals.get("recall", fallback_vals["recall"])
    f1 = report_vals.get("f1", fallback_vals["f1"])
    model_version = report_vals.get("model_version") or fallback_vals["model_version"]

    return ModelMetrics(
        pr_auc=round(float(pr_auc), 4),
        latency_ms=round(float(latency_ms), 3),
        precision=round(float(precision), 4) if precision is not None else None,
        recall=round(float(recall), 4) if recall is not None else None,
        f1=round(float(f1), 4) if f1 is not None else None,
        model_version=model_version,
    )


def evaluate_candidate(
    candidate: ModelMetrics,
    champion: ModelMetrics | None,
    gate: MetricsGate = DEFAULT_METRICS_GATE,
) -> EvaluationResult:
    """Evaluate candidate model metrics against the baseline gate and current champion."""
    reasons: list[str] = []

    # 1. Baseline PR-AUC gate
    if candidate.pr_auc < gate.min_pr_auc:
        msg = (
            f"Candidate PR-AUC ({candidate.pr_auc:.4f}) below baseline gate ({gate.min_pr_auc:.4f})"
        )
        reasons.append(msg)

    # 2. Baseline latency gate
    if candidate.latency_ms > gate.max_latency_ms:
        msg = (
            f"Candidate latency ({candidate.latency_ms:.2f} ms) "
            f"exceeds baseline gate ({gate.max_latency_ms:.2f} ms)"
        )
        reasons.append(msg)

    # 3. Challenger vs Champion comparison
    if champion is not None:
        if gate.require_better_or_equal_pr_auc and candidate.pr_auc < champion.pr_auc:
            msg = (
                f"Challenger PR-AUC ({candidate.pr_auc:.4f}) is lower than "
                f"incumbent Champion ({champion.pr_auc:.4f})"
            )
            reasons.append(msg)
        elif candidate.pr_auc == champion.pr_auc and candidate.latency_ms > champion.latency_ms * (
            1 + gate.max_latency_regression_pct / 100
        ):
            msg = (
                f"Challenger latency ({candidate.latency_ms:.2f} ms) regressed compared "
                f"to Champion ({champion.latency_ms:.2f} ms) without PR-AUC gain"
            )
            reasons.append(msg)

    passed = len(reasons) == 0
    return EvaluationResult(
        passed=passed,
        candidate_metrics=candidate,
        champion_metrics=champion,
        reasons=tuple(reasons),
    )


def promote_model(
    run_id: str,
    model_name: str = DEFAULT_REGISTERED_MODEL_NAME,
    alias: str = DEFAULT_CHAMPION_ALIAS,
    gate: MetricsGate = DEFAULT_METRICS_GATE,
    tracking_uri: str | None = None,
    local_dir: Path = DEFAULT_LOCAL_DIR,
    export_dir: Path | None = None,
) -> PromotionResult:
    """Evaluate a candidate model run against baseline gates and promote to MLflow registry."""
    target = resolve_tracking_target(tracking_uri, local_dir)
    client = MlflowClient(tracking_uri=target.uri)

    candidate_run = client.get_run(run_id)
    candidate_metrics = extract_metrics(candidate_run, client)

    # Ensure registered model exists
    try:
        client.get_registered_model(model_name)
    except Exception:
        try:
            client.create_registered_model(
                model_name, description="PBIP Fraud Detection Hybrid Scorer"
            )
        except Exception as exc:
            _logger.debug("Registered model creation handled: %s", exc)

    # Retrieve current champion metrics if present
    champion_metrics: ModelMetrics | None = None
    existing_champion_version: str | None = None
    try:
        current_champ = client.get_model_version_by_alias(model_name, alias)
        existing_champion_version = str(current_champ.version)
        if current_champ.run_id:
            champ_run = client.get_run(current_champ.run_id)
            champion_metrics = extract_metrics(champ_run, client)
    except Exception as exc:
        _logger.info("No current champion alias '%s' found: %s", alias, exc)

    evaluation = evaluate_candidate(candidate_metrics, champion_metrics, gate)
    if not evaluation.passed:
        return PromotionResult(
            promoted=False,
            candidate_version=None,
            candidate_run_id=run_id,
            registered_model_name=model_name,
            alias=alias,
            evaluation=evaluation,
            exported_path=None,
        )

    # Candidate passed: register model version and tag alias
    artifact_uri = candidate_run.info.artifact_uri
    desc = (
        f"Promoted to {alias} (PR-AUC: {candidate_metrics.pr_auc:.4f}, "
        f"latency: {candidate_metrics.latency_ms:.2f}ms)"
    )
    model_version = client.create_model_version(
        name=model_name,
        source=artifact_uri,
        run_id=run_id,
        description=desc,
    )

    # Re-tag previous champion as challenger if applicable
    if existing_champion_version and existing_champion_version != str(model_version.version):
        try:
            client.set_registered_model_alias(
                name=model_name,
                alias=DEFAULT_CHALLENGER_ALIAS,
                version=existing_champion_version,
            )
        except Exception as exc:
            _logger.warning("Could not set challenger alias on previous champion: %s", exc)

    # Tag candidate as champion
    client.set_registered_model_alias(
        name=model_name,
        alias=alias,
        version=str(model_version.version),
    )
    client.set_model_version_tag(
        name=model_name,
        version=str(model_version.version),
        key="promotion_status",
        value=alias,
    )
    client.set_model_version_tag(
        name=model_name,
        version=str(model_version.version),
        key="promoted_at",
        value=datetime.now(UTC).isoformat(),
    )
    client.set_model_version_tag(
        name=model_name,
        version=str(model_version.version),
        key="pr_auc",
        value=str(candidate_metrics.pr_auc),
    )
    client.set_model_version_tag(
        name=model_name,
        version=str(model_version.version),
        key="latency_ms",
        value=str(candidate_metrics.latency_ms),
    )

    # Optional local artifact export
    exported_path: str | None = None
    if export_dir is not None:
        export_dir.mkdir(parents=True, exist_ok=True)
        download_artifacts(
            run_id=run_id,
            tracking_uri=target.uri,
            dst_path=str(export_dir.resolve()),
        )
        exported_path = str(export_dir.resolve())

    return PromotionResult(
        promoted=True,
        candidate_version=str(model_version.version),
        candidate_run_id=run_id,
        registered_model_name=model_name,
        alias=alias,
        evaluation=evaluation,
        exported_path=exported_path,
    )


def _find_latest_run(experiment_name: str, client: MlflowClient) -> str:
    exp = client.get_experiment_by_name(experiment_name)
    if exp is None:
        raise ValueError(f"Experiment '{experiment_name}' not found")
    runs = client.search_runs(
        experiment_ids=[exp.experiment_id],
        order_by=["attributes.start_time DESC"],
        max_results=1,
    )
    if not runs:
        raise ValueError(f"No runs found in experiment '{experiment_name}'")
    return str(runs[0].info.run_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", type=str, default=None, help="MLflow Run ID of candidate model")
    parser.add_argument(
        "--latest-run",
        action="store_true",
        help="Use the latest run from the experiment as candidate",
    )
    parser.add_argument(
        "--experiment",
        type=str,
        default="pbip-fraud",
        help="Experiment name when using --latest-run",
    )
    parser.add_argument(
        "--model-name",
        type=str,
        default=DEFAULT_REGISTERED_MODEL_NAME,
        help="Registered model name in MLflow registry",
    )
    parser.add_argument(
        "--alias",
        type=str,
        default=DEFAULT_CHAMPION_ALIAS,
        help="Alias to assign to winning model (default 'champion')",
    )
    parser.add_argument(
        "--min-pr-auc",
        type=float,
        default=DEFAULT_MIN_PR_AUC,
        help=f"Baseline PR-AUC gate (default {DEFAULT_MIN_PR_AUC})",
    )
    parser.add_argument(
        "--max-latency-ms",
        type=float,
        default=DEFAULT_MAX_LATENCY_MS,
        help=f"Baseline latency gate in ms (default {DEFAULT_MAX_LATENCY_MS})",
    )
    parser.add_argument("--tracking-uri", type=str, default=None, help="MLflow tracking URI")
    parser.add_argument(
        "--local-dir",
        type=Path,
        default=DEFAULT_LOCAL_DIR,
        help="Local SQLite fallback directory",
    )
    parser.add_argument(
        "--export-dir",
        type=Path,
        default=None,
        help="Optional local path to export champion artifacts",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with code 1 if candidate is not promoted",
    )
    args = parser.parse_args(argv)

    run_id = args.run_id
    if not run_id:
        if args.latest_run:
            target = resolve_tracking_target(args.tracking_uri, args.local_dir)
            client = MlflowClient(tracking_uri=target.uri)
            try:
                run_id = _find_latest_run(args.experiment, client)
            except Exception as exc:
                sys.stderr.write(f"Error finding latest run: {exc}\n")
                return 1
        else:
            parser.error("Either --run-id or --latest-run must be provided")

    gate = MetricsGate(min_pr_auc=args.min_pr_auc, max_latency_ms=args.max_latency_ms)
    result = promote_model(
        run_id=run_id,
        model_name=args.model_name,
        alias=args.alias,
        gate=gate,
        tracking_uri=args.tracking_uri,
        local_dir=args.local_dir,
        export_dir=args.export_dir,
    )

    sys.stdout.write(json.dumps(result.as_dict(), indent=2) + "\n")
    if args.strict and not result.promoted:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
