"""Serving side of the hybrid fraud scorer (TASK-033, ADR-0019): load once, score one transfer.

This is the only ``pbip_ml.fraud`` module the API imports besides the contract. It never trains:
it loads the artifacts written by ``python -m pbip_ml.fraud.train`` and turns one transfer's
platform data into the exact feature row the models were trained on (the TASK-024 online
features, proven identical to the offline pipeline, plus amount/hour/device/direction).

Artifact safety: ``isolation_forest.joblib`` is a pickle, and unpickling runs code. Load only
from an operator-controlled directory (``FRAUD_MODEL_DIR``), never from a path a request can
influence. ``manifest.json`` (SHA-256 per file, written by the training CLI) is checked first,
so a corrupted or half-copied artifact is refused. It does not stop someone who can write the
directory, because they could rewrite the manifest too (threat model T29).
"""

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import joblib
import lightgbm as lgb
import pandas as pd

from pbip_ml.fraud.features import CurrentTransfer, PastTransfer, online_features
from pbip_ml.fraud.hybrid import HybridParams, HybridResult, HybridScorer
from pbip_ml.fraud.rules import RuleContext, default_registry
from pbip_ml.fraud.training import AnomalyModel

ARTIFACTS = ("lightgbm.txt", "isolation_forest.joblib", "hybrid_params.json")
MANIFEST = "manifest.json"
IST = timedelta(hours=5, minutes=30)


class ModelLoadError(Exception):
    """The artifacts are missing, do not match the manifest, or cannot be read."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_manifest(directory: Path) -> dict[str, str]:
    """Record the SHA-256 of every artifact (called by the training CLI after writing them)."""
    digests = {name: _sha256(directory / name) for name in ARTIFACTS}
    (directory / MANIFEST).write_text(json.dumps(digests, indent=2) + "\n", encoding="utf-8")
    return digests


def load(directory: Path) -> HybridScorer:
    """Verify the manifest, then load the three artifacts into a ready scorer."""
    try:
        expected = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
        for name in ARTIFACTS:
            if expected.get(name) != _sha256(directory / name):
                raise ModelLoadError(f"{name} does not match {MANIFEST}")
        params = HybridParams.from_json((directory / "hybrid_params.json").read_text("utf-8"))
        booster = lgb.Booster(model_file=str(directory / "lightgbm.txt"))
        # Unpickling runs code: only ever an operator-set directory (see the module docstring).
        anomaly = joblib.load(directory / "isolation_forest.joblib")
    except ModelLoadError:
        raise
    except (OSError, ValueError, TypeError, KeyError, lgb.basic.LightGBMError) as exc:
        raise ModelLoadError(type(exc).__name__) from exc
    if not isinstance(anomaly, AnomalyModel):
        raise ModelLoadError("isolation_forest.joblib is not an AnomalyModel")
    return HybridScorer(params, booster, anomaly, default_registry())


def load_from_registry(
    registry_name: str = "pbip-fraud",
    alias: str = "champion",
    tracking_uri: str | None = None,
    cache_dir: Path | None = None,
) -> HybridScorer:
    """Download model artifacts for an alias ('champion') from MLflow Model Registry and load them.

    Raises ModelLoadError if the MLflow server is unreachable, the model alias is not found,
    or the downloaded artifacts do not match the manifest.
    """
    import os

    from mlflow.artifacts import download_artifacts
    from mlflow.tracking import MlflowClient

    from pbip_ml.tracking import (
        DEFAULT_TRACKING_URI,
        TRACKING_URI_ENV,
        resolve_tracking_target,
        server_is_reachable,
    )

    candidate = tracking_uri or os.environ.get(TRACKING_URI_ENV) or DEFAULT_TRACKING_URI
    if candidate.startswith(("http://", "https://")) and not server_is_reachable(candidate):
        raise ModelLoadError(f"MLflow server at {candidate} is unreachable")

    target = resolve_tracking_target(tracking_uri)

    client = MlflowClient(tracking_uri=target.uri)
    try:
        model_version = client.get_model_version_by_alias(registry_name, alias)
    except Exception as exc:
        raise ModelLoadError(
            f"Alias '{alias}' not found in registry '{registry_name}': {exc}"
        ) from exc

    run_id = model_version.run_id
    if not run_id:
        raise ModelLoadError(f"Model version {model_version.version} has no run_id")

    dest_path = str(cache_dir.resolve()) if cache_dir is not None else None
    try:
        downloaded = download_artifacts(run_id=run_id, tracking_uri=target.uri, dst_path=dest_path)
    except Exception as exc:
        raise ModelLoadError(f"Failed to download artifacts for run {run_id}: {exc}") from exc

    downloaded_dir = Path(downloaded)
    if not (downloaded_dir / MANIFEST).is_file():
        candidates = list(downloaded_dir.rglob(MANIFEST))
        if candidates:
            downloaded_dir = candidates[0].parent
        else:
            raise ModelLoadError(f"{MANIFEST} not found in downloaded artifacts")

    return load(downloaded_dir)


@dataclass(frozen=True, slots=True)
class TransferInputs:
    """What the platform knows about one outgoing transfer at decision time."""

    amount_minor: int
    currency: str
    occurred_at: datetime
    payee_added_at: datetime | None
    device_id: UUID | None
    new_device: bool
    failed_logins_24h: int
    history: Sequence[PastTransfer]


def feature_row(inputs: TransferInputs) -> tuple[pd.DataFrame, RuleContext]:
    """The model's input row and the rule context, built exactly as in training."""
    features = online_features(
        CurrentTransfer(
            occurred_at=inputs.occurred_at,
            direction="DEBIT",
            amount_minor=inputs.amount_minor,
            payee_added_at=inputs.payee_added_at,
            device_id=inputs.device_id,
            failed_logins_24h=inputs.failed_logins_24h,
        ),
        inputs.history,
    )
    row = pd.DataFrame(
        [
            {
                **features.as_dict(),
                "log_amount": math.log10(inputs.amount_minor),
                # To UTC first, as training does: an aware non-UTC time would shift the hour.
                "hour_ist": (inputs.occurred_at.astimezone(UTC) + IST).hour,
                "new_device": int(inputs.new_device),
                "is_debit": 1,
            }
        ]
    )
    return row, RuleContext(inputs.amount_minor, inputs.currency, features, inputs.new_device)


def score_transfer(scorer: HybridScorer, inputs: TransferInputs) -> HybridResult:
    row, context = feature_row(inputs)
    return scorer.score(row, context)
