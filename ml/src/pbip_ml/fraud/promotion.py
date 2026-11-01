"""Champion/challenger promotion in the MLflow Model Registry (TASK-038). Offline only.

Two commands (``python -m pbip_ml.fraud.promotion``):

``promote``
    Takes a training run (``python -m pbip_ml.fraud.train`` logs ``lightgbm.txt``,
    ``isolation_forest.joblib``, ``hybrid_params.json``, ``manifest.json`` and ``report.json``) and
    MEASURES it: the artifacts are downloaded, checked against their manifest and loaded with the
    serving loader (``pbip_ml.fraud.inference.load``), then scored on the labelled export's
    time-split test period (and the held-out seed when given) and timed one transfer at a time,
    with the same functions as the evaluation report (``pbip_ml.fraud.evaluation``). The numbers
    the training run reported about itself are never trusted. The current champion is measured on
    the same rows. The run is registered as a new version with the ``challenger`` alias, and moves
    to ``champion`` only if it passes the gate:

    - served-hybrid PR-AUC >= ``min_pr_auc`` on the test period (and on the held-out seed if given);
    - single-request p95 latency of ``score_transfer`` <= ``max_latency_p95_ms`` (default 50 ms,
      the API's ``FRAUD_MODEL_BUDGET_MS``: a slower model would mostly be cut off by the budget);
    - PR-AUC not lower than the current champion's by more than ``max_pr_auc_drop``.

    Every version keeps its measured numbers and the verdict as tags, so the registry is the audit
    trail of what was promoted and why.

``export-champion``
    The API does not talk to MLflow (ADR-0019: it loads checked files once at startup). This copies
    the champion's artifacts into the served directory (``models/fraud``), after checking that
    their manifest is byte-identical to the one the gate measured and that the serving loader
    accepts them. The swap is a directory rename, so the API sees the old complete set or the new
    complete set. If MLflow is unreachable or anything fails, the directory is left untouched and
    the API keeps starting from the last exported champion (the local-disk fallback).
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from mlflow.artifacts import download_artifacts
from mlflow.entities.model_registry import ModelVersion
from mlflow.exceptions import MlflowException
from mlflow.tracking import MlflowClient

from pbip_ml.fraud import training as t
from pbip_ml.fraud.evaluation import latency_profile, served_metrics
from pbip_ml.fraud.hybrid import HybridScorer
from pbip_ml.fraud.inference import ARTIFACTS, MANIFEST, ModelLoadError, load
from pbip_ml.fraud.train import EXPERIMENT, load_export
from pbip_ml.tracking import DEFAULT_TRACKING_URI, TRACKING_URI_ENV, server_is_reachable

REGISTERED_MODEL = "pbip-fraud"
CHAMPION = "champion"
CHALLENGER = "challenger"
REPORT = "report.json"
PROVENANCE = "champion.json"
RUN_FILES = (*ARTIFACTS, MANIFEST, REPORT)
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_SCHEMES = ("http://", "https://", "sqlite:///")

_logger = logging.getLogger(__name__)


class PromotionError(Exception):
    """The registry is unreachable, a run or alias is missing, or the artifacts are not trusted."""


@dataclass(frozen=True, slots=True)
class MetricsGate:
    min_pr_auc: float = 0.80  # the served hybrid: 0.860 test / 0.858 held-out (report v1.1)
    max_latency_p95_ms: float = 50.0  # = FRAUD_MODEL_BUDGET_MS default (ADR-0019)
    max_pr_auc_drop: float = 0.0  # vs the champion, measured on the same rows


@dataclass(frozen=True, slots=True)
class Measured:
    """What the gate measured for one model (never copied from the run's own report)."""

    pr_auc_test: float
    recall_at_review: float
    fpr_at_review: float
    latency_p95_ms: float
    pr_auc_holdout: float | None = None
    rows_test: int = 0  # outgoing transfers in the test period (``build_dataset``)


@dataclass(frozen=True, slots=True)
class GateResult:
    passed: bool
    reasons: tuple[str, ...]
    candidate: Measured
    champion_version: str | None = None
    champion_pr_auc_test: float | None = None


@dataclass(frozen=True, slots=True)
class PromotionResult:
    model_name: str
    run_id: str
    version: str
    promoted: bool
    champion_version: str | None  # read back from the registry after the decision
    gate: GateResult

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExportResult:
    model_name: str
    version: str
    run_id: str
    out_dir: str
    manifest_sha256: str
    files: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- registry access
def tracking_uri_from(explicit: str | None, environ: Mapping[str, str]) -> str:
    """Argument -> ``MLFLOW_TRACKING_URI`` -> the local server. No silent fallback to a local store:
    promoting into, or exporting from, another registry than intended is worse than failing."""
    uri = explicit or environ.get(TRACKING_URI_ENV) or DEFAULT_TRACKING_URI
    if not uri.startswith(_SCHEMES):
        raise PromotionError("tracking URI must be http(s):// or sqlite:///")
    return uri


def connect(uri: str) -> MlflowClient:
    if uri.startswith(("http://", "https://")) and not server_is_reachable(uri):
        raise PromotionError("MLflow server is unreachable")
    return MlflowClient(tracking_uri=uri, registry_uri=uri)


def _check_name(name: str) -> str:
    if not _NAME.match(name):
        raise PromotionError("registered model name must match [A-Za-z0-9][A-Za-z0-9._-]{0,99}")
    return name


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch_run_artifacts(uri: str, run_id: str, dst: Path) -> HybridScorer:
    """Download the fixed artifact names of ``run_id`` (never paths read from the registry) and
    load them with the serving loader, which refuses files that do not match ``manifest.json``."""
    dst.mkdir(parents=True, exist_ok=True)
    for name in RUN_FILES:
        try:
            download_artifacts(
                run_id=run_id, artifact_path=name, dst_path=str(dst), tracking_uri=uri
            )
        except (MlflowException, OSError) as exc:
            raise PromotionError(f"run {run_id} has no {name}") from exc
    try:
        return load(dst)
    except ModelLoadError as exc:
        raise PromotionError(f"run {run_id} artifacts refused by the loader: {exc}") from exc


def _aliases(client: MlflowClient, name: str) -> dict[str, str]:
    try:
        model = client.get_registered_model(name)
    except MlflowException:
        return {}
    return {alias: str(version) for alias, version in model.aliases.items()}


def _ensure_registered_model(client: MlflowClient, name: str) -> None:
    try:
        client.get_registered_model(name)
    except MlflowException:
        client.create_registered_model(name, description="PBIP hybrid fraud scorer (TASK-038)")


# --------------------------------------------------------------------------- measuring
def measure(
    scorer: HybridScorer, test: pd.DataFrame, holdout: pd.DataFrame | None, latency_calls: int
) -> Measured:
    on_test = served_metrics(scorer, test)
    on_holdout = served_metrics(scorer, holdout) if holdout is not None else None
    latency = latency_profile(scorer, workers=(1,), calls=latency_calls)["threads_1"]
    return Measured(
        pr_auc_test=on_test.pr_auc,
        recall_at_review=on_test.recall,
        fpr_at_review=on_test.fpr,
        latency_p95_ms=float(latency["p95_ms"]),
        pr_auc_holdout=on_holdout.pr_auc if on_holdout is not None else None,
        rows_test=on_test.rows,
    )


def gate_reasons(
    candidate: Measured, champion_pr_auc: float | None, gate: MetricsGate
) -> tuple[str, ...]:
    """Every failed condition, in words an operator can act on. Empty means promote."""
    reasons = []
    floor = gate.min_pr_auc
    if candidate.pr_auc_test < floor:
        reasons.append(f"PR-AUC {candidate.pr_auc_test:.4f} on the test period < gate {floor:.4f}")
    if candidate.pr_auc_holdout is not None and candidate.pr_auc_holdout < floor:
        reasons.append(
            f"PR-AUC {candidate.pr_auc_holdout:.4f} on the held-out seed < gate {floor:.4f}"
        )
    if candidate.latency_p95_ms > gate.max_latency_p95_ms:
        reasons.append(
            f"p95 latency {candidate.latency_p95_ms:.2f} ms > gate {gate.max_latency_p95_ms:.2f} ms"
        )
    if (
        champion_pr_auc is not None
        and candidate.pr_auc_test < champion_pr_auc - gate.max_pr_auc_drop
    ):
        reasons.append(
            f"PR-AUC {candidate.pr_auc_test:.4f} < the champion's {champion_pr_auc:.4f} "
            "on the same rows"
        )
    return tuple(reasons)


def _check_same_export(report_path: Path, test: pd.DataFrame, run_id: str) -> None:
    """The test period must be one the run never trained on. ``report.json`` is used only to check
    that the evaluation data is the export this run was trained on, never for the gate's numbers."""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    expected = report.get("time_ranges", {}).get("test")
    actual = [str(test["occurred_at"].min()), str(test["occurred_at"].max())]
    if expected != actual:
        raise PromotionError(
            f"--data is not the export run {run_id} was trained on (test periods differ); "
            "evaluating on it could score rows the model trained on"
        )


def _champion_pr_auc(
    uri: str, client: MlflowClient, name: str, version: str | None, test: pd.DataFrame, work: Path
) -> float | None:
    if version is None:
        return None
    champion = client.get_model_version(name, version)
    if not champion.run_id:
        raise PromotionError(f"champion version {version} has no run")
    scorer = fetch_run_artifacts(uri, champion.run_id, work / "champion")
    return served_metrics(scorer, test).pr_auc


# --------------------------------------------------------------------------- promote
def _register(client: MlflowClient, name: str, run_id: str, manifest_sha: str) -> ModelVersion:
    run = client.get_run(run_id)
    _ensure_registered_model(client, name)
    version = client.create_model_version(
        name=name,
        source=run.info.artifact_uri,
        run_id=run_id,
        tags={"manifest_sha256": manifest_sha, "gate": "pending"},
        description="Hybrid fraud scorer candidate (pbip_ml.fraud.promotion)",
    )
    client.set_registered_model_alias(name, CHALLENGER, str(version.version))
    return version


def _record(
    client: MlflowClient, name: str, version: str, gate: MetricsGate, result: GateResult
) -> None:
    measured = {k: v for k, v in asdict(result.candidate).items() if v is not None}
    tags = {
        "gate": "passed" if result.passed else "failed",
        "gate_reasons": "; ".join(result.reasons) or "-",
        "gate_thresholds": json.dumps(asdict(gate), sort_keys=True),
        "measured": json.dumps(measured, sort_keys=True),
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "compared_with_champion": result.champion_version or "-",
    }
    for key, value in tags.items():
        client.set_model_version_tag(name, version, key, value)


def promote(
    uri: str,
    run_id: str,
    data: Path,
    holdout: Path | None = None,
    model_name: str = REGISTERED_MODEL,
    gate: MetricsGate | None = None,
    latency_calls: int = 400,
) -> PromotionResult:
    """Measure ``run_id`` and the champion, register the run as challenger, promote on a pass."""
    gate = gate or MetricsGate()
    name = _check_name(model_name)
    client = connect(uri)
    test = t.time_split(load_export(data)).test
    held = load_export(holdout) if holdout is not None else None
    with tempfile.TemporaryDirectory(prefix="pbip-promote-") as tmp:
        work = Path(tmp)
        scorer = fetch_run_artifacts(uri, run_id, work / "candidate")
        _check_same_export(work / "candidate" / REPORT, test, run_id)
        manifest_sha = _sha256(work / "candidate" / MANIFEST)
        champion_version = _aliases(client, name).get(CHAMPION)
        champion_pr_auc = _champion_pr_auc(uri, client, name, champion_version, test, work)
        candidate = measure(scorer, test, held, latency_calls)
    reasons = gate_reasons(candidate, champion_pr_auc, gate)
    result = GateResult(not reasons, reasons, candidate, champion_version, champion_pr_auc)
    version = str(_register(client, name, run_id, manifest_sha).version)
    _record(client, name, version, gate, result)
    if result.passed:
        client.set_registered_model_alias(name, CHAMPION, version)
        client.delete_registered_model_alias(name, CHALLENGER)
    after = _aliases(client, name).get(CHAMPION)
    if result.passed and after != version:
        raise PromotionError(f"alias {CHAMPION} reads back as {after}, expected {version}")
    _logger.info(
        "fraud model gate decided",
        extra={"model": name, "version": version, "promoted": result.passed},
    )
    return PromotionResult(name, run_id, version, result.passed, after, result)


def latest_training_run(client: MlflowClient, experiment: str = EXPERIMENT) -> str:
    found = client.get_experiment_by_name(experiment)
    if found is None:
        raise PromotionError(f"experiment {experiment} not found")
    runs = client.search_runs(
        [found.experiment_id],
        "attributes.status = 'FINISHED'",
        max_results=1,
        order_by=["attributes.start_time DESC"],
    )
    if not runs:
        raise PromotionError(f"no finished run in experiment {experiment}")
    return str(runs[0].info.run_id)


# --------------------------------------------------------------------------- export
def _swap_into(staging: Path, out_dir: Path, parking: Path) -> None:
    """Replace ``out_dir`` with ``staging`` by two renames, restoring the old one on failure."""
    had_old = out_dir.exists()
    if had_old:
        out_dir.rename(parking)
    try:
        staging.rename(out_dir)
    except OSError:
        if had_old:
            parking.rename(out_dir)
        raise


def _champion_version(client: MlflowClient, name: str) -> ModelVersion:
    version_id = _aliases(client, name).get(CHAMPION)
    if version_id is None:
        raise PromotionError(f"{name} has no {CHAMPION} alias")
    version = client.get_model_version(name, version_id)
    if version.tags.get("gate") != "passed" or not version.run_id:
        raise PromotionError(f"{name} version {version_id} did not pass the gate")
    return version


def export_champion(uri: str, out_dir: Path, model_name: str = REGISTERED_MODEL) -> ExportResult:
    """Copy the champion's artifacts into ``out_dir`` (the API's ``FRAUD_MODEL_DIR``)."""
    name = _check_name(model_name)
    version = _champion_version(connect(uri), name)
    run_id = str(version.run_id)
    out_dir = out_dir.resolve()
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out_dir.parent, prefix=f".{out_dir.name}-") as tmp:
        staging = Path(tmp) / "new"
        fetch_run_artifacts(uri, run_id, staging)  # the serving loader verifies the manifest
        manifest_sha = _sha256(staging / MANIFEST)
        if manifest_sha != version.tags.get("manifest_sha256"):
            raise PromotionError("champion artifacts differ from the ones the gate measured")
        provenance = {
            "registered_model": name,
            "version": str(version.version),
            "run_id": run_id,
            "alias": CHAMPION,
            "manifest_sha256": manifest_sha,
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        (staging / PROVENANCE).write_text(json.dumps(provenance, indent=2) + "\n", "utf-8")
        _swap_into(staging, out_dir, Path(tmp) / "previous")
    load(out_dir)  # exactly what the API does at startup
    files = tuple(sorted(p.name for p in out_dir.iterdir()))
    return ExportResult(name, str(version.version), run_id, str(out_dir), manifest_sha, files)


# --------------------------------------------------------------------------- CLI
def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--tracking-uri", default=None, help="default: $MLFLOW_TRACKING_URI")
    common.add_argument("--model-name", default=REGISTERED_MODEL)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser(
        "promote", parents=[common], help="measure a training run, promote it if it passes"
    )
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("--run-id")
    which.add_argument("--latest-run", action="store_true", help=f"newest run in {EXPERIMENT}")
    p.add_argument("--data", type=Path, required=True, help="the export the run was trained on")
    p.add_argument("--holdout", type=Path, default=None, help="an export from another seed")
    defaults = MetricsGate()
    p.add_argument("--min-pr-auc", type=float, default=defaults.min_pr_auc)
    p.add_argument("--max-latency-ms", type=float, default=defaults.max_latency_p95_ms)
    p.add_argument("--max-pr-auc-drop", type=float, default=defaults.max_pr_auc_drop)
    p.add_argument("--latency-calls", type=int, default=400)
    e = sub.add_parser(
        "export-champion", parents=[common], help="copy the champion into the served model dir"
    )
    e.add_argument("--out", type=Path, required=True, help="the API's FRAUD_MODEL_DIR")
    return parser


def _run(args: argparse.Namespace, uri: str) -> int:
    if args.command == "export-champion":
        exported = export_champion(uri, args.out, args.model_name)
        sys.stdout.write(json.dumps(exported.as_dict(), indent=2) + "\n")
        return 0
    run_id = args.run_id or latest_training_run(connect(uri))
    gate = MetricsGate(args.min_pr_auc, args.max_latency_ms, args.max_pr_auc_drop)
    result = promote(
        uri, run_id, args.data, args.holdout, args.model_name, gate, args.latency_calls
    )
    sys.stdout.write(json.dumps(result.as_dict(), indent=2) + "\n")
    return 0 if result.promoted else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0: promoted / exported. 1: the gate rejected the run. 2: could not run."""
    args = _parser().parse_args(argv)
    try:
        return _run(args, tracking_uri_from(args.tracking_uri, os.environ))
    except (PromotionError, MlflowException, OSError) as exc:
        sys.stderr.write(f"promotion failed: {exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
