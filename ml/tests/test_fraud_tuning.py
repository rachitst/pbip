"""TASK-084: the bounded hyper-parameter search never sees the test period and is reproducible."""

from pathlib import Path

import pandas as pd
import pytest
from pbip_datagen.__main__ import _export_fraud
from pbip_datagen.generator import GeneratorConfig
from pbip_ml.fraud import training as t
from pbip_ml.fraud.train import load_export

from scripts import fraud_tune


@pytest.fixture(scope="module")
def development(tmp_path_factory: pytest.TempPathFactory) -> pd.DataFrame:
    """Training + validation periods of a small export: what the search may use."""
    out = tmp_path_factory.mktemp("tune")
    _export_fraud(GeneratorConfig(seed=42, customers=600, months=6), out)
    split = t.time_split(load_export(out))
    return pd.concat([split.train, split.validation])


def test_folds_are_expanding_and_always_fit_on_the_past(development: pd.DataFrame) -> None:
    folds = fraud_tune.time_folds(development, blocks=5)
    assert len(folds) == 3
    sizes = [len(fit) for fit, _ in folds]
    assert sizes == sorted(sizes) and len(set(sizes)) == 3
    for fit, held in folds:
        assert fit["occurred_at"].max() < held["occurred_at"].min()
        assert len(held) and set(fit.index).isdisjoint(held.index)


def test_search_starts_from_the_current_params_and_is_reproducible(
    development: pd.DataFrame,
) -> None:
    first = fraud_tune.search(development, trials=2)
    assert first == fraud_tune.search(development, trials=2)  # fixed sampler seed
    assert len(first["all"]) == 2
    assert first["all"][0]["learning_rate"] == t.LGBM_PARAMS["learning_rate"]  # trial 0 = today
    assert first["best_pr_auc"] == max(trial["value"] for trial in first["all"])
    assert 0 < first["current_params_pr_auc"] <= 1
    assert set(first["best_params"]) == set(fraud_tune.TUNED)


def test_search_space_covers_learning_rate_depth_and_regularisation() -> None:
    assert {"learning_rate", "max_depth", "lambda_l1", "lambda_l2"} <= set(fraud_tune.TUNED)
    assert set(fraud_tune.current_params()) == set(fraud_tune.TUNED)


def test_cli_writes_the_result_and_a_params_file(tmp_path: Path) -> None:
    out = tmp_path / "data"
    out.mkdir()
    _export_fraud(GeneratorConfig(seed=3, customers=300, months=6), out)
    result, best = tmp_path / "tune.json", tmp_path / "best.json"
    argv = ["fraud_tune", "--data", str(out), "--trials", "1", "--json", str(result),
            "--best-params", str(best)]  # fmt: skip
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("sys.argv", argv)
        fraud_tune.main()
    assert result.exists() and best.exists()
