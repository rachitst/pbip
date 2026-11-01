"""TASK-029/030: time split, LightGBM and Isolation Forest on labelled synthetic data."""

import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pbip_datagen.__main__ import _export_fraud
from pbip_datagen.generator import GeneratorConfig
from pbip_ml.fraud import train as train_cli
from pbip_ml.fraud import training as t
from pbip_ml.fraud.train import load_export, log_to_mlflow, run
from pbip_ml.tracking import TrackingTarget, local_target


@pytest.fixture(scope="module")
def exported(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("fraud")
    _export_fraud(GeneratorConfig(seed=42, customers=1500, months=6), out)
    return out


@pytest.fixture(scope="module")
def dataset(exported: Path) -> pd.DataFrame:
    return load_export(exported)


def test_split_is_chronological_with_no_overlap(dataset: pd.DataFrame) -> None:
    split = t.time_split(dataset)
    assert split.train["occurred_at"].max() < split.validation["occurred_at"].min()
    assert split.validation["occurred_at"].max() < split.test["occurred_at"].min()
    assert len(split.train) + len(split.validation) + len(split.test) == len(dataset)
    assert all(part["label"].sum() > 0 for part in (split.train, split.validation, split.test))


def test_metrics_from_a_known_confusion_matrix() -> None:
    m = t.evaluate(np.array([1, 1, 0, 0, 0]), np.array([0.9, 0.2, 0.8, 0.1, 0.1]), 0.5)
    assert (m.tp, m.fn, m.fp, m.tn) == (1, 1, 1, 2)
    assert (m.precision, m.recall, m.f1, m.fnr) == (0.5, 0.5, 0.5, 0.5)
    assert m.fpr == round(1 / 3, 6)


def test_lightgbm_meets_the_metric_gate_and_beats_the_anomaly_model(dataset: pd.DataFrame) -> None:
    split = t.time_split(dataset)
    booster, threshold = t.train_lightgbm(split)
    x_test, y_test = t._xy(split.test)
    scores = booster.predict(x_test, num_iteration=booster.best_iteration)
    lgbm = t.evaluate(y_test, scores, threshold)
    anomaly = t.train_isolation_forest(split)
    iso = t.evaluate(y_test, anomaly.score(split.test), 0.5)
    assert lgbm.pr_auc >= 0.85 and lgbm.recall >= 0.8 and lgbm.fpr <= 0.01  # metric gate
    assert lgbm.pr_auc > iso.pr_auc
    assert 0 < threshold < 1


def test_behaviour_known_fraud_scores_high_and_normal_low(dataset: pd.DataFrame) -> None:
    """Behaviour regression on the held-back TEST period: labelled fraud vs ordinary transfers."""
    split = t.time_split(dataset)
    booster, _ = t.train_lightgbm(split)
    fraud = split.test[split.test["label"] == 1]
    normal = split.test[split.test["label"] == 0]
    fraud_scores = np.asarray(booster.predict(t._xy(fraud)[0]))
    normal_scores = np.asarray(booster.predict(t._xy(normal)[0]))
    assert np.median(fraud_scores) >= 0.9  # known fraud scores >= X
    assert np.quantile(normal_scores, 0.99) <= 0.1  # known normal scores <= Y


def test_isolation_forest_scores_are_normalised_explained_and_fast(dataset: pd.DataFrame) -> None:
    split = t.time_split(dataset)
    anomaly = t.train_isolation_forest(split)
    scores = anomaly.score(split.test)
    assert scores.min() >= 0.0 and scores.max() <= 1.0
    worst = split.test.iloc[[int(np.argmax(scores))]]
    reason = anomaly.explain(worst.iloc[0])
    assert reason.split(" is ")[0] in t.ANOMALY_FEATURES and "usually about" in reason
    cpu = t.single_prediction_ms(anomaly.score, worst, repeats=50, clock=time.process_time)
    assert cpu["p95_ms"] < 25


def test_training_is_deterministic(dataset: pd.DataFrame) -> None:
    split = t.time_split(dataset)
    a, _ = t.train_lightgbm(split)
    b, _ = t.train_lightgbm(split)
    x = t._xy(split.test)[0]
    assert np.array_equal(a.predict(x), b.predict(x))


def test_cli_run_writes_artifacts_and_logs_to_mlflow(
    exported: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_shared_server(*_args: object, **_kwargs: object) -> TrackingTarget:
        # Regression: with a local MLflow server on :5000 the test used to log there, not to tmp_path.
        raise AssertionError("test must not resolve (or probe) the shared tracking server")

    monkeypatch.setattr(train_cli, "configure_tracking", _no_shared_server)
    out = tmp_path / "models"
    report = run(exported, out, holdout=exported)
    assert {p.name for p in out.iterdir()} == {
        "report.json",
        "lightgbm.txt",
        "isolation_forest.joblib",
        "hybrid_params.json",
        "manifest.json",  # SHA-256 per artifact, verified by the serving loader (TASK-033)
    }
    assert report["lightgbm"]["test"]["pr_auc"] > 0 and "holdout_seed" in report["lightgbm"]
    assert report["hybrid"]["at_review"]["recall"] >= report["rules_only"]["at_review"]["recall"]
    run_id = log_to_mlflow(report, out, tmp_path / "mlruns", local_target(tmp_path / "mlruns"))
    assert run_id and any((tmp_path / "mlruns").rglob("lightgbm.txt"))
