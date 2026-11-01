"""TASK-038: MLflow Model Registry Champion/Challenger promotion workflow tests."""

import json
from pathlib import Path

import pytest
from mlflow.tracking import MlflowClient
from pbip_ml.fraud.inference import ModelLoadError, load_from_registry
from pbip_ml.fraud.promotion import (
    MetricsGate,
    ModelMetrics,
    evaluate_candidate,
    extract_metrics,
    main,
    promote_model,
)
from pbip_ml.tracking import ensure_experiment, local_target

pytestmark = pytest.mark.unit


def test_metrics_gate_passes_for_valid_candidate() -> None:
    candidate = ModelMetrics(pr_auc=0.88, latency_ms=18.5, precision=0.85, recall=0.82, f1=0.83)
    gate = MetricsGate(min_pr_auc=0.85, max_latency_ms=25.0)
    result = evaluate_candidate(candidate, champion=None, gate=gate)
    assert result.passed is True
    assert len(result.reasons) == 0


def test_metrics_gate_fails_when_pr_auc_below_threshold() -> None:
    candidate = ModelMetrics(pr_auc=0.74, latency_ms=15.0)
    gate = MetricsGate(min_pr_auc=0.85, max_latency_ms=25.0)
    result = evaluate_candidate(candidate, champion=None, gate=gate)
    assert result.passed is False
    assert any("PR-AUC" in r and "below baseline gate" in r for r in result.reasons)


def test_metrics_gate_fails_when_latency_exceeds_threshold() -> None:
    candidate = ModelMetrics(pr_auc=0.92, latency_ms=34.2)
    gate = MetricsGate(min_pr_auc=0.85, max_latency_ms=25.0)
    result = evaluate_candidate(candidate, champion=None, gate=gate)
    assert result.passed is False
    assert any("latency" in r and "exceeds baseline gate" in r for r in result.reasons)


def test_challenger_rejected_if_pr_auc_regresses_against_champion() -> None:
    champion = ModelMetrics(pr_auc=0.89, latency_ms=15.0)
    challenger = ModelMetrics(pr_auc=0.86, latency_ms=12.0)  # meets gate, but worse than champion
    gate = MetricsGate(min_pr_auc=0.85, max_latency_ms=25.0, require_better_or_equal_pr_auc=True)
    result = evaluate_candidate(challenger, champion=champion, gate=gate)
    assert result.passed is False
    assert any("lower than incumbent Champion" in r for r in result.reasons)


def test_challenger_promoted_when_beating_champion() -> None:
    champion = ModelMetrics(pr_auc=0.86, latency_ms=20.0)
    challenger = ModelMetrics(pr_auc=0.91, latency_ms=18.0)
    gate = MetricsGate(min_pr_auc=0.85, max_latency_ms=25.0)
    result = evaluate_candidate(challenger, champion=champion, gate=gate)
    assert result.passed is True
    assert len(result.reasons) == 0


def test_extract_metrics_reads_report_json(tmp_path: Path) -> None:
    target = local_target(tmp_path)
    client = MlflowClient(tracking_uri=target.uri)
    exp = ensure_experiment("test-extract", target)
    run = client.create_run(exp)

    report_content = {
        "lightgbm": {
            "test": {"pr_auc": 0.892, "precision": 0.85, "recall": 0.81, "f1": 0.83},
            "latency": {"p95_ms": 14.8},
        },
        "hybrid": {"params": {"model_version": "hybrid-v1"}},
    }
    report_file = tmp_path / "report.json"
    report_file.write_text(json.dumps(report_content), encoding="utf-8")
    client.log_artifact(run.info.run_id, str(report_file))

    metrics = extract_metrics(run, client)
    assert metrics.pr_auc == 0.892
    assert metrics.latency_ms == 14.8
    assert metrics.precision == 0.85
    assert metrics.model_version == "hybrid-v1"


def test_promote_model_creates_version_and_sets_champion_alias(tmp_path: Path) -> None:
    target = local_target(tmp_path)
    client = MlflowClient(tracking_uri=target.uri)
    exp = ensure_experiment("test-promote", target)
    run = client.create_run(exp)

    client.log_metric(run.info.run_id, "lgbm_pr_auc", 0.90)
    client.log_metric(run.info.run_id, "latency_p95_ms", 12.5)

    dummy_artifact = tmp_path / "dummy.txt"
    dummy_artifact.write_text("ok", encoding="utf-8")
    client.log_artifact(run.info.run_id, str(dummy_artifact))

    result = promote_model(
        run_id=run.info.run_id,
        model_name="test-fraud-model",
        alias="champion",
        gate=MetricsGate(min_pr_auc=0.85, max_latency_ms=25.0),
        tracking_uri=target.uri,
    )

    assert result.promoted is True
    assert str(result.candidate_version) == "1"

    # Verify alias retrieval from MLflow
    champ = client.get_model_version_by_alias("test-fraud-model", "champion")
    assert str(champ.version) == "1"
    assert champ.run_id == run.info.run_id


def test_promote_model_rejects_failing_candidate(tmp_path: Path) -> None:
    target = local_target(tmp_path)
    client = MlflowClient(tracking_uri=target.uri)
    exp = ensure_experiment("test-promote-fail", target)
    run = client.create_run(exp)

    client.log_metric(run.info.run_id, "lgbm_pr_auc", 0.70)  # below gate
    client.log_metric(run.info.run_id, "latency_p95_ms", 12.5)

    result = promote_model(
        run_id=run.info.run_id,
        model_name="test-fraud-fail",
        alias="champion",
        gate=MetricsGate(min_pr_auc=0.85, max_latency_ms=25.0),
        tracking_uri=target.uri,
    )

    assert result.promoted is False
    assert result.candidate_version is None
    assert len(result.evaluation.reasons) > 0


def test_challenger_promotion_replaces_champion_and_sets_challenger_alias(tmp_path: Path) -> None:
    target = local_target(tmp_path)
    client = MlflowClient(tracking_uri=target.uri)
    exp = ensure_experiment("test-promote-replace", target)

    # 1. Promote champion v1
    run1 = client.create_run(exp)
    client.log_metric(run1.info.run_id, "lgbm_pr_auc", 0.86)
    client.log_metric(run1.info.run_id, "latency_p95_ms", 15.0)
    res1 = promote_model(
        run_id=run1.info.run_id,
        model_name="pbip-fraud-comp",
        alias="champion",
        tracking_uri=target.uri,
    )
    assert res1.promoted is True
    assert str(res1.candidate_version) == "1"

    # 2. Evaluate challenger v2 with higher PR-AUC
    run2 = client.create_run(exp)
    client.log_metric(run2.info.run_id, "lgbm_pr_auc", 0.92)
    client.log_metric(run2.info.run_id, "latency_p95_ms", 14.0)
    res2 = promote_model(
        run_id=run2.info.run_id,
        model_name="pbip-fraud-comp",
        alias="champion",
        tracking_uri=target.uri,
    )
    assert res2.promoted is True
    assert str(res2.candidate_version) == "2"

    # The new champion should be v2
    champion = client.get_model_version_by_alias("pbip-fraud-comp", "champion")
    assert str(champion.version) == "2"

    # The old champion should be tagged as challenger
    challenger = client.get_model_version_by_alias("pbip-fraud-comp", "challenger")
    assert str(challenger.version) == "1"


def test_load_from_registry_loads_artifacts_properly(tmp_path: Path) -> None:
    """Train minimal real artifacts, log to run, promote to champion, and load via load_from_registry."""
    from pbip_datagen.__main__ import _export_fraud
    from pbip_datagen.generator import GeneratorConfig
    from pbip_ml.fraud.train import run as train_run

    data_dir = tmp_path / "data"
    models_dir = tmp_path / "models"
    data_dir.mkdir(parents=True, exist_ok=True)
    models_dir.mkdir(parents=True, exist_ok=True)
    _export_fraud(GeneratorConfig(seed=42, customers=200, months=2), data_dir)
    train_run(data_dir, models_dir)

    target = local_target(tmp_path / "mlflow")
    client = MlflowClient(tracking_uri=target.uri)
    exp = ensure_experiment("test-load-registry", target)
    run = client.create_run(exp)

    for item in models_dir.iterdir():
        client.log_artifact(run.info.run_id, str(item))

    client.log_metric(run.info.run_id, "lgbm_pr_auc", 0.95)
    client.log_metric(run.info.run_id, "latency_p95_ms", 10.0)

    promote_res = promote_model(
        run_id=run.info.run_id,
        model_name="fraud-reg-test",
        alias="champion",
        gate=MetricsGate(min_pr_auc=0.05, max_latency_ms=100.0),
        tracking_uri=target.uri,
    )
    assert promote_res.promoted is True

    # Load from registry using 'champion' alias
    scorer = load_from_registry(
        registry_name="fraud-reg-test",
        alias="champion",
        tracking_uri=target.uri,
        cache_dir=tmp_path / "cache",
    )
    assert scorer is not None
    assert scorer.params.model_version == "hybrid-lgbm-iforest-1"


def test_load_from_registry_fails_gracefully_when_alias_missing(tmp_path: Path) -> None:
    target = local_target(tmp_path)
    with pytest.raises(ModelLoadError, match="not found"):
        load_from_registry(
            registry_name="nonexistent-model",
            alias="champion",
            tracking_uri=target.uri,
        )


def test_load_from_registry_fails_gracefully_when_server_unreachable() -> None:
    with pytest.raises(ModelLoadError, match="unreachable"):
        load_from_registry(
            registry_name="pbip-fraud",
            alias="champion",
            tracking_uri="http://127.0.0.1:1",
        )


def test_promotion_cli_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = local_target(tmp_path)
    client = MlflowClient(tracking_uri=target.uri)
    exp = ensure_experiment("cli-exp", target)
    run = client.create_run(exp)
    client.log_metric(run.info.run_id, "lgbm_pr_auc", 0.91)
    client.log_metric(run.info.run_id, "latency_p95_ms", 10.0)

    exit_code = main(
        [
            "--run-id",
            run.info.run_id,
            "--model-name",
            "cli-model",
            "--tracking-uri",
            target.uri,
            "--min-pr-auc",
            "0.85",
            "--strict",
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr().out
    data = json.loads(captured)
    assert data["promoted"] is True
    assert data["candidate_version"] == "1"


def test_promotion_cli_strict_fails_on_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = local_target(tmp_path)
    client = MlflowClient(tracking_uri=target.uri)
    exp = ensure_experiment("cli-exp-fail", target)
    run = client.create_run(exp)
    client.log_metric(run.info.run_id, "lgbm_pr_auc", 0.70)
    client.log_metric(run.info.run_id, "latency_p95_ms", 10.0)

    exit_code = main(
        [
            "--run-id",
            run.info.run_id,
            "--model-name",
            "cli-model-fail",
            "--tracking-uri",
            target.uri,
            "--min-pr-auc",
            "0.85",
            "--strict",
        ]
    )
    assert exit_code == 1
    captured = capsys.readouterr().out
    data = json.loads(captured)
    assert data["promoted"] is False
