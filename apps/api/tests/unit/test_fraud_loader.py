"""TASK-038 / ADR-0019: Fast unit tests for load_engine dynamic registry loading and offline fallback."""

from pathlib import Path
from typing import Any

import pytest
from pbip_api.core.config import Settings
from pbip_api.main import create_app
from pbip_api.modules.fraud.hybrid import load_engine
from pydantic import SecretStr

pytestmark = pytest.mark.unit


@pytest.fixture
def base_settings() -> Settings:
    return Settings(
        app_env="local",
        jwt_secret=SecretStr("x" * 48),
        field_encryption_key=SecretStr("y" * 44),
        fraud_scorer="hybrid",
        mlflow_tracking_uri="",
        fraud_registered_model_name="",
        fraud_model_dir=None,
    )


@pytest.fixture(scope="module")
def model_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from pbip_datagen.__main__ import _export_fraud
    from pbip_datagen.generator import GeneratorConfig
    from pbip_ml.fraud.train import run

    data, out = tmp_path_factory.mktemp("fraud-data"), tmp_path_factory.mktemp("fraud-model")
    _export_fraud(GeneratorConfig(seed=42, customers=150, months=2), data)
    run(data, out)
    return out


def test_load_engine_returns_none_when_rules_only(base_settings: Settings) -> None:
    settings = base_settings.model_copy(update={"fraud_scorer": "rules"})
    assert load_engine(settings) is None


def test_load_engine_returns_none_when_no_registry_and_no_disk(base_settings: Settings) -> None:
    assert load_engine(base_settings) is None


def test_load_engine_loads_from_local_disk(base_settings: Settings, model_dir: Path) -> None:
    settings = base_settings.model_copy(update={"fraud_model_dir": str(model_dir)})
    engine = load_engine(settings)
    assert engine is not None
    assert engine.params.model_version == "hybrid-lgbm-iforest-1"


def test_load_engine_dynamically_loads_from_mlflow_registry_champion(
    base_settings: Settings, model_dir: Path, tmp_path: Path
) -> None:
    """TASK-038: Dynamic loading from MLflow Model Registry alias 'champion'."""
    from mlflow.tracking import MlflowClient
    from pbip_ml.fraud.promotion import MetricsGate, promote_model
    from pbip_ml.tracking import ensure_experiment, local_target

    target = local_target(tmp_path / "mlflow")
    client = MlflowClient(tracking_uri=target.uri)
    exp = ensure_experiment("api-loader-test", target)
    run = client.create_run(exp)

    for item in model_dir.iterdir():
        client.log_artifact(run.info.run_id, str(item))
    client.log_metric(run.info.run_id, "lgbm_pr_auc", 0.95)
    client.log_metric(run.info.run_id, "latency_p95_ms", 12.0)

    promote_model(
        run_id=run.info.run_id,
        model_name="pbip-fraud",
        alias="champion",
        gate=MetricsGate(min_pr_auc=0.01, max_latency_ms=100.0),
        tracking_uri=target.uri,
    )

    settings = base_settings.model_copy(
        update={
            "mlflow_tracking_uri": target.uri,
            "fraud_registered_model_name": "pbip-fraud",
            "fraud_model_alias": "champion",
            "fraud_model_cache_dir": str(tmp_path / "cache"),
            "fraud_model_dir": None,
        }
    )
    engine = load_engine(settings)
    assert engine is not None
    assert engine.params.model_version == "hybrid-lgbm-iforest-1"


def test_load_engine_falls_back_to_disk_when_mlflow_unreachable(
    base_settings: Settings, model_dir: Path
) -> None:
    """TASK-038: When MLflow server is unreachable, falls back to FRAUD_MODEL_DIR."""
    settings = base_settings.model_copy(
        update={
            "mlflow_tracking_uri": "http://127.0.0.1:1",
            "fraud_registered_model_name": "pbip-fraud",
            "fraud_model_alias": "champion",
            "fraud_model_dir": str(model_dir),
        }
    )
    engine = load_engine(settings)
    assert engine is not None
    assert engine.params.model_version == "hybrid-lgbm-iforest-1"


def test_broken_model_stack_falls_back_to_rules_without_crashing_app(
    base_settings: Settings, model_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    real_import = builtins.__import__

    def failing_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "pbip_ml.fraud.inference":
            raise OSError("libgomp.so.1: cannot open shared object file")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    settings = base_settings.model_copy(update={"fraud_model_dir": str(model_dir)})
    assert load_engine(settings) is None
    assert create_app(settings).state.fraud_engine is None
