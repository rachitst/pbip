"""Importing the API must not load ML training code (AGENTS.md section 2: the API depends only on
contracts and inference modules). Regression for the segmentation package re-exporting its pipeline,
which pulled scikit-learn into every API process at startup.

The probe runs with the fraud scorer pinned, never with whatever the developer's ``.env`` says: since
1 Nov 2026 ``.env.example`` (and so every fresh ``make setup``) turns on ``FRAUD_SCORER=hybrid``, whose
served Isolation Forest IS a scikit-learn model (ADR-0019). The probe then read that ``.env`` and saw
scikit-learn, which is how this test failed on a freshly set-up Windows PC (#329).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PROBE = """
import sys
import pbip_api.main
import pbip_api.worker.tasks
loaded = sorted(m for m in sys.modules if m == "sklearn" or m.startswith("sklearn.") or m.startswith("pbip_ml."))
print("LOADED:" + ",".join(loaded))
"""
# The fresh-setup values that made this test depend on the machine it ran on (#329).
_HYBRID_DOTENV = "".join(
    f"{line}\n" for line in ("FRAUD_SCORER=hybrid", "FRAUD_MODEL_DIR=models/fraud")
)
# Modules that only training jobs may load, in any scorer mode.
_TRAINING_ONLY = ("pbip_ml.segmentation.pipeline", "pbip_ml.fraud.train")


def _loaded_modules(cwd: Path, **settings: str) -> list[str]:
    """The sklearn and pbip_ml modules an API + worker import loads, run from ``cwd`` (where the
    settings look for ``.env``) with ``settings`` as environment variables, which beat ``.env``."""
    result = subprocess.run(  # noqa: S603 - runs the interpreter on a fixed probe string
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        cwd=cwd,
        env={**os.environ, **settings},
    )
    assert result.returncode == 0, result.stderr[-2000:]
    line = next(line for line in result.stdout.splitlines() if line.startswith("LOADED:"))
    return [name for name in line.removeprefix("LOADED:").split(",") if name]


def test_importing_the_api_and_worker_does_not_load_sklearn_or_the_segmentation_pipeline(
    tmp_path: Path,
) -> None:
    """With the rules scorer (the code default) no API or worker process loads scikit-learn, even
    when the ``.env`` in the working directory asks for the hybrid scorer."""
    (tmp_path / ".env").write_text(_HYBRID_DOTENV, encoding="utf-8")

    loaded = _loaded_modules(tmp_path, FRAUD_SCORER="rules", FRAUD_MODEL_DIR="")

    sklearn = [m for m in loaded if m == "sklearn" or m.startswith("sklearn.")]
    assert sklearn == [], f"training code reached the request path: {sklearn}"
    assert not set(_TRAINING_ONLY) & set(loaded)


def test_the_hybrid_scorer_still_never_loads_the_training_jobs(tmp_path: Path) -> None:
    """The hybrid scorer needs scikit-learn to unpickle its Isolation Forest (ADR-0019), but the
    segmentation pipeline and the fraud training CLI stay off the request path."""
    loaded = _loaded_modules(
        tmp_path, FRAUD_SCORER="hybrid", FRAUD_MODEL_DIR=str(tmp_path / "no-models")
    )

    assert "pbip_ml.fraud.inference" in loaded  # the hybrid path really was taken
    assert not set(_TRAINING_ONLY) & set(loaded)
