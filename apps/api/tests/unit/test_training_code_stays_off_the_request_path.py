"""Importing the API must not load ML training code (AGENTS.md section 2: the API depends only on
contracts and inference modules). Regression for the segmentation package re-exporting its pipeline,
which pulled scikit-learn into every API process at startup."""

import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

PROBE = """
import sys
import pbip_api.main
import pbip_api.worker.tasks
loaded = sorted(m for m in sys.modules if m == "sklearn" or m.startswith("sklearn.") or m == "pbip_ml.segmentation.pipeline")
print("LOADED:" + ",".join(loaded))
"""


def test_importing_the_api_and_worker_does_not_load_sklearn_or_the_segmentation_pipeline() -> None:
    result = subprocess.run(  # noqa: S603 - runs the interpreter on a fixed probe string
        [sys.executable, "-c", PROBE], capture_output=True, text=True, check=False, timeout=120
    )
    assert result.returncode == 0, result.stderr[-2000:]
    line = next(line for line in result.stdout.splitlines() if line.startswith("LOADED:"))
    assert line == "LOADED:", f"training code reached the request path: {line}"
