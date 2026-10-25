"""MLflow tracking client helper with a local fallback.

Resolution order for the tracking URI: explicit argument -> ``MLFLOW_TRACKING_URI`` (MLflow's own
convention) -> ``http://localhost:5000`` (``docker compose --profile ml up``). An HTTP(S) server is
used only if its ``/health`` endpoint answers; otherwise runs go to a local SQLite store
(``<local_dir>/mlflow.db``, artifacts in ``<local_dir>/artifacts``) so training and notebooks never
fail just because the server is down. The fallback is logged, never silent. (MLflow 3.x refuses
new writes to the old ``./mlruns`` file store, hence SQLite.)

Offline/training code only - never imported on the API request path.
"""

import logging
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import mlflow
from mlflow.tracking import MlflowClient

DEFAULT_TRACKING_URI = "http://localhost:5000"
TRACKING_URI_ENV = "MLFLOW_TRACKING_URI"
DEFAULT_LOCAL_DIR = Path("mlruns")  # git-ignored
_PROBE_TIMEOUT_SECONDS = 2.0

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TrackingTarget:
    uri: str
    is_remote: bool
    artifact_location: str | None = None  # set for the local fallback only


def server_is_reachable(uri: str, timeout: float = _PROBE_TIMEOUT_SECONDS) -> bool:
    """True if an MLflow server answers ``GET <uri>/health`` with 200."""
    if not uri.startswith(("http://", "https://")):
        return False
    try:
        # Scheme is restricted to http(s) above; the URI is operator configuration.
        with urllib.request.urlopen(f"{uri.rstrip('/')}/health", timeout=timeout) as response:  # noqa: S310  # nosec B310
            return bool(response.status == 200)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def local_target(local_dir: Path) -> TrackingTarget:
    directory = local_dir.resolve()
    (directory / "artifacts").mkdir(parents=True, exist_ok=True)
    return TrackingTarget(
        uri=f"sqlite:///{directory / 'mlflow.db'}",
        is_remote=False,
        artifact_location=(directory / "artifacts").as_uri(),
    )


def resolve_tracking_target(
    uri: str | None = None,
    local_dir: Path = DEFAULT_LOCAL_DIR,
    probe: Callable[[str], bool] = server_is_reachable,
) -> TrackingTarget:
    candidate = uri or os.environ.get(TRACKING_URI_ENV) or DEFAULT_TRACKING_URI
    if not candidate.startswith(("http://", "https://")):
        return TrackingTarget(uri=candidate, is_remote=False)  # explicitly local store
    if probe(candidate):
        return TrackingTarget(uri=candidate, is_remote=True)
    target = local_target(local_dir)
    _logger.warning(
        "mlflow server unreachable, using local tracking store",
        extra={"requested": candidate, "fallback": target.uri},
    )
    return target


def configure_tracking(
    uri: str | None = None,
    local_dir: Path = DEFAULT_LOCAL_DIR,
    probe: Callable[[str], bool] = server_is_reachable,
) -> TrackingTarget:
    """Resolve the target and point the global ``mlflow`` client at it."""
    target = resolve_tracking_target(uri, local_dir, probe)
    mlflow.set_tracking_uri(target.uri)
    return target


def ensure_experiment(name: str, target: TrackingTarget) -> str:
    """Experiment id for ``name``, creating it (with the fallback's artifact root) if missing."""
    client = MlflowClient(tracking_uri=target.uri)
    existing = client.get_experiment_by_name(name)
    if existing is not None:
        return str(existing.experiment_id)
    return str(client.create_experiment(name, artifact_location=target.artifact_location))
