"""#350: with ``FRAUD_SCORER=hybrid`` and real artifacts, the API loads the models without importing
the fraud training module (AGENTS.md section 2; ADR-0033).

The artifact is pickled under the pre-ADR-0033 class path ``pbip_ml.fraud.training.AnomalyModel``,
so this also proves an old artifact loads without pulling training in. The probe runs in a fresh
interpreter with a minimal environment from an empty working directory: no developer ``.env`` and
no inherited ``GIT_*`` or settings variables can change the outcome.
"""

import os
import subprocess
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
import pbip_api
import pbip_ml
import pytest
from joblib import dump
from pbip_ml.fraud.artifacts import ANOMALY_FEATURES, MODEL_FEATURES, AnomalyModel
from pbip_ml.fraud.hybrid import HybridParams
from pbip_ml.fraud.inference import write_manifest
from sklearn.ensemble import IsolationForest

pytestmark = pytest.mark.unit

MODEL_VERSION = "test-legacy-artifact"
PROBE = """
import sys
import pbip_api.main
engine = pbip_api.main.app.state.fraud_engine
print("ENGINE:" + ("none" if engine is None else engine.params.model_version))
print("LOADED:" + ",".join(sorted(m for m in sys.modules if m.startswith("pbip_ml.fraud."))))
"""
# The offline fraud jobs. None of them may be imported by an API process in any scorer mode.
FRAUD_TRAINING = {
    "pbip_ml.fraud.evaluation",
    "pbip_ml.fraud.promotion",
    "pbip_ml.fraud.train",
    "pbip_ml.fraud.training",
}


def _legacy_artifacts(directory: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A small, valid, seeded model directory whose anomaly pickle uses the legacy class path."""
    rng = np.random.default_rng(7)
    frame = pd.DataFrame(rng.random((200, len(MODEL_FEATURES))), columns=list(MODEL_FEATURES))
    labels = (frame["log_amount"] > 0.8).astype(int)
    booster = lgb.train(
        {"objective": "binary", "verbose": -1, "seed": 7, "num_threads": 1},
        lgb.Dataset(frame, labels),
        num_boost_round=5,
    )
    booster.save_model(str(directory / "lightgbm.txt"))
    x = frame[list(ANOMALY_FEATURES)]
    forest = IsolationForest(n_estimators=10, random_state=7).fit(x.to_numpy(dtype=float))
    anomaly = AnomalyModel(
        forest,
        low=0.3,
        high=0.7,
        medians={n: float(x[n].median()) for n in ANOMALY_FEATURES},
        spreads=dict.fromkeys(ANOMALY_FEATURES, 0.5),
    )
    with monkeypatch.context() as patch:  # how every artifact was pickled before ADR-0033
        patch.setattr(AnomalyModel, "__module__", "pbip_ml.fraud.training")
        dump(anomaly, directory / "isolation_forest.joblib")
    params = HybridParams(MODEL_VERSION, [0.0, 1.0], [0.0, 1.0], -3.0, 2.0, 1.0, 1.0)
    (directory / "hybrid_params.json").write_text(params.to_json(), encoding="utf-8")
    write_manifest(directory)
    return directory


def _clean_env(**settings: str) -> dict[str, str]:
    """Only what an interpreter needs, plus ``settings``. The source roots are the ones this test
    imported, so the probe checks this checkout's code, not whatever is installed elsewhere."""
    roots = [Path(m.__file__).resolve().parents[1] for m in (pbip_api, pbip_ml) if m.__file__]
    env = {"PYTHONPATH": os.pathsep.join(str(r) for r in roots), **settings}
    for name in ("PATH", "SYSTEMROOT"):  # SYSTEMROOT: Windows sockets need it
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def test_hybrid_scorer_loads_a_legacy_artifact_without_importing_fraud_training(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    models = tmp_path / "models"
    models.mkdir()
    _legacy_artifacts(models, monkeypatch)
    assert b"pbip_ml.fraud.training" in (models / "isolation_forest.joblib").read_bytes()
    cwd = tmp_path / "cwd"
    cwd.mkdir()

    result = subprocess.run(  # noqa: S603 - runs the interpreter on a fixed probe string
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
        cwd=cwd,
        env=_clean_env(FRAUD_SCORER="hybrid", FRAUD_MODEL_DIR=str(models)),
    )

    assert result.returncode == 0, result.stderr[-2000:]
    lines = dict(line.split(":", 1) for line in result.stdout.splitlines() if ":" in line)
    assert lines.get("ENGINE") == MODEL_VERSION, result.stdout[-2000:] + result.stderr[-2000:]
    loaded = set(lines["LOADED"].split(","))
    assert "pbip_ml.fraud.inference" in loaded  # the hybrid path really was taken
    assert not FRAUD_TRAINING & loaded, f"training code reached the request path: {loaded}"
