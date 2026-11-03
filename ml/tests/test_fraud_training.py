"""TASK-029/030: time split, LightGBM and Isolation Forest on labelled synthetic data."""

import json
from pathlib import Path

import mlflow
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


def test_dataset_holds_outgoing_transfers_only_with_features_from_all_rows() -> None:
    """TASK-071: the API scores debits only, so training/evaluation rows exclude credits."""
    at = pd.Timestamp("2025-03-01 10:00", tz="UTC")
    device = "d1"
    rows = [  # a debit, a big credit (e.g. an F7 deposit), then a debit
        ("a", at, "DEBIT", 1_000_00),
        ("b", at + pd.Timedelta(hours=1), "CREDIT", 99_500_00),
        ("c", at + pd.Timedelta(hours=2), "DEBIT", 2_000_00),
    ]
    transfers = pd.DataFrame(
        {
            "id": [r[0] for r in rows],
            "customer_id": "c1",
            "occurred_at": [r[1] for r in rows],
            "direction": [r[2] for r in rows],
            "amount_minor": [r[3] for r in rows],
            "payee_added_at": [at - pd.Timedelta(days=90), pd.NaT, at - pd.Timedelta(days=90)],
            "device_id": device,
            "new_device": False,
            "failed_logins_24h": 0,
        }
    )
    labels = pd.DataFrame({"transaction_id": ["b"], "fraudulent": [True]})
    data = t.build_dataset(transfers, labels)
    assert list(data.index) == [0, 2]  # the credit is gone; positions in the export are kept
    assert data["is_debit"].eq(1).all() and data["label"].eq(0).all()
    assert data.loc[2, "ratio_to_avg_amount"] == 2.0  # history (the earlier debit) still counts


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


def test_isolation_forest_scores_are_normalised_explained_and_cheap(dataset: pd.DataFrame) -> None:
    split = t.time_split(dataset)
    anomaly = t.train_isolation_forest(split)
    scores = anomaly.score(split.test)
    assert scores.min() >= 0.0 and scores.max() <= 1.0
    worst = split.test.iloc[[int(np.argmax(scores))]]
    reason = anomaly.explain(worst.iloc[0])
    assert reason.split(" is ")[0] in t.ANOMALY_FEATURES and "usually about" in reason
    # #362: the cost of one row, fixed by the forest's shape rather than timed (a CPU-time bound
    # failed under a full local run on a 2-CPU laptop): 100 trees of at most 256 samples, so at
    # most 8 levels each, scored in this thread. Real time: scripts/benchmark_fraud_latency.py
    # ("anomaly" pass); the promotion gate times the whole model before it is served.
    forest = anomaly.forest
    assert len(forest.estimators_) == 100 and forest.max_samples_ <= 256
    assert max(tree.get_depth() for tree in forest.estimators_) <= 8
    assert forest.n_jobs is None  # no worker pool per call


def test_training_is_deterministic(dataset: pd.DataFrame) -> None:
    split = t.time_split(dataset)
    a, _ = t.train_lightgbm(split)
    b, _ = t.train_lightgbm(split)
    x = t._xy(split.test)[0]
    assert np.array_equal(a.predict(x), b.predict(x))


# Hang guard, not a speed bar (as #362): the full pipeline on 1,500 customers now also trains the
# three out-of-time calibration folds (#396), well past 60 s on a busy 2-CPU laptop.
@pytest.mark.timeout(300)
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
    assert report["hybrid"]["params"]["model_version"] == train_cli.HYBRID_VERSION
    assert report["lightgbm"]["params"]["learning_rate"] == t.LGBM_PARAMS["learning_rate"]
    calibration = report["hybrid"]["calibration"]  # #396: fitted on more than validation alone
    assert calibration["positives"] > calibration["validation_positives"] > 0
    shap = report["lightgbm"]["shap_mean_abs_test"]  # TASK-084: SHAP summary in the logged report
    assert list(shap) == sorted(shap, key=lambda f: -shap[f]) and set(shap) == set(t.MODEL_FEATURES)
    assert all(v >= 0 for v in shap.values()) and max(shap.values()) > 0
    logged = mlflow.MlflowClient(local_target(tmp_path / "mlruns").uri).get_run(run_id)
    assert logged.data.params["model_version"] == train_cli.HYBRID_VERSION
    assert any(k.startswith("shap_mean_abs_") for k in logged.data.metrics)


def test_hybrid_version_tells_the_recalibrated_model_apart() -> None:
    """TASK-084: both earlier models were served as ``-1``; a retrain must be told apart. #396
    changed how the calibration and the stack are fitted, so it is ``-3``, not the ``-2`` run."""
    assert train_cli.HYBRID_VERSION == "hybrid-lgbm-iforest-4"


def test_calibration_scores_come_from_models_that_never_saw_those_rows_or_later_ones(
    dataset: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#396: every out-of-time score is made by a fold model trained only on EARLIER training
    rows; no validation or test row is used, and the scores carry more positives than validation."""
    split = t.time_split(dataset)
    folds: list[tuple[pd.Index, pd.Index]] = []
    real_train = t.lgb.train

    def spy(params: dict[str, object], train_set: t.lgb.Dataset, **kwargs: object) -> object:
        seen = train_set.data.index  # before training: LightGBM frees the raw data after it
        model = real_train(params, train_set, **kwargs)

        class Recording:
            def predict(self, x: pd.DataFrame) -> object:
                folds.append((seen, x.index))
                return model.predict(x)

        assert kwargs["num_boost_round"] == 37  # the final model's number of trees
        return Recording()

    monkeypatch.setattr(t.lgb, "train", spy)
    scores, labels = t.out_of_time_scores(split, rounds=37)
    assert len(folds) == t.CALIBRATION_FOLDS - 1
    when = split.train["occurred_at"]
    for fitted_on, scored in folds:
        assert when[fitted_on].max() < when[scored].min()  # fitted on the past only
    scored_all = pd.Index(np.concatenate([s.to_numpy() for _, s in folds]))
    assert scored_all.is_unique and set(scored_all) <= set(split.train.index)
    assert set(scored_all).isdisjoint(split.validation.index) and set(scored_all).isdisjoint(
        split.test.index
    )
    assert len(scores) == len(labels) == len(scored_all)
    assert labels.tolist() == split.train.loc[scored_all, "label"].tolist()
    assert labels.sum() > split.validation["label"].sum()  # the point: more positives


def test_lightgbm_params_override_is_used_and_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tuning candidate (``--lgbm-params``) is what trains, and the report says so."""

    def _local_only(local_dir: Path) -> TrackingTarget:  # never probe a shared server
        target = local_target(local_dir)
        mlflow.set_tracking_uri(target.uri)
        return target

    monkeypatch.setattr(train_cli, "configure_tracking", _local_only)
    small = tmp_path / "data"
    small.mkdir()
    _export_fraud(GeneratorConfig(seed=3, customers=300, months=6), small)  # small: CLI timing
    overrides = tmp_path / "params.json"
    overrides.write_text('{"learning_rate": 0.1, "max_depth": 3, "num_leaves": 7}', "utf-8")
    out = tmp_path / "models"
    train_cli.main(["--data", str(small), "--out", str(out), "--lgbm-params", str(overrides),
                    "--mlflow-local-dir", str(tmp_path / "mlruns")])  # fmt: skip
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["lightgbm"]["params"]["learning_rate"] == 0.1
    model = (out / "lightgbm.txt").read_text(encoding="utf-8")
    assert "[max_depth: 3]" in model and "[num_leaves: 7]" in model  # what LightGBM trained with
