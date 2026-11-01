"""TASK-038: champion/challenger promotion against a real (local SQLite) MLflow registry.

Candidates are real training runs (``pbip_ml.fraud.train.run`` + ``log_to_mlflow``): a good model, and
a model trained on labels moved to random transfers (no signal) whose report CLAIMS a PR-AUC of 0.99.
The gate must measure both itself - same functions as the evaluation report - and promote only the
good one. Exported champions must pass the serving loader's checksum check.
"""

import hashlib
import json
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import mlflow
import numpy as np
import pandas as pd
import pytest
from mlflow.tracking import MlflowClient
from pbip_datagen.__main__ import _export_fraud
from pbip_datagen.generator import GeneratorConfig
from pbip_ml.fraud import inference as inf
from pbip_ml.fraud import promotion as p
from pbip_ml.fraud import training as t
from pbip_ml.fraud.evaluation import served_metrics
from pbip_ml.fraud.train import load_export, log_to_mlflow, run
from pbip_ml.tracking import TRACKING_URI_ENV, ensure_experiment, local_target

pytestmark = [pytest.mark.unit, pytest.mark.timeout(240)]

GATE = p.MetricsGate(min_pr_auc=0.6, max_latency_p95_ms=1_000.0)  # small export: 0.74 vs 0.33
CALLS = 30  # latency samples per measurement (400 in the real CLI)


@dataclass(frozen=True)
class Registry:
    uri: str
    data: Path
    good_run: str
    good_dir: Path
    bad_run: str


def _train_and_log(data: Path, out: Path, local_dir: Path, claim: float | None = None) -> str:
    report = run(data, out)
    if claim is not None:  # the run lies about itself: the gate must not care
        report["lightgbm"]["test"]["pr_auc"] = claim
        (out / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return log_to_mlflow(report, out, local_dir)


@pytest.fixture(scope="module")
def registry(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Registry]:
    root = tmp_path_factory.mktemp("mlflow")
    target = local_target(root)
    ensure_experiment("pbip-fraud", target)  # artifacts under tmp, not the working directory
    data, bad_data = tmp_path_factory.mktemp("data"), tmp_path_factory.mktemp("bad-data")
    _export_fraud(GeneratorConfig(seed=42, customers=600, months=4), data)
    shutil.copy(data / "transfers.csv", bad_data / "transfers.csv")
    transfers, labels = pd.read_csv(data / "transfers.csv"), pd.read_csv(data / "fraud_labels.csv")
    rng = np.random.default_rng(7)
    labels["transaction_id"] = rng.choice(transfers["id"].to_numpy(), len(labels), replace=False)
    labels.to_csv(bad_data / "fraud_labels.csv", index=False)
    previous = mlflow.get_tracking_uri()
    with pytest.MonkeyPatch.context() as env:
        env.setenv(TRACKING_URI_ENV, target.uri)
        good_dir = tmp_path_factory.mktemp("good-model")
        good = _train_and_log(data, good_dir, root)
        bad = _train_and_log(bad_data, tmp_path_factory.mktemp("bad-model"), root, claim=0.99)
        yield Registry(target.uri, data, good, good_dir, bad)
    mlflow.set_tracking_uri(previous)


def _promote(
    reg: Registry, run_id: str, name: str, gate: p.MetricsGate = GATE
) -> p.PromotionResult:
    return p.promote(reg.uri, run_id, reg.data, model_name=name, gate=gate, latency_calls=CALLS)


def _client(reg: Registry) -> MlflowClient:
    return MlflowClient(tracking_uri=reg.uri, registry_uri=reg.uri)


def _aliases(reg: Registry, name: str) -> dict[str, str]:
    """Read back from the registry (MLflow returns version numbers as ints here)."""
    return {k: str(v) for k, v in _client(reg).get_registered_model(name).aliases.items()}


# --------------------------------------------------------------------------- the gate rules
def _measured(pr_auc: float, latency: float = 10.0, holdout: float | None = None) -> p.Measured:
    return p.Measured(pr_auc, 0.8, 0.001, latency, holdout)


@pytest.mark.parametrize(
    ("measured", "champion", "expected"),
    [
        (_measured(0.86), None, ()),
        (_measured(0.79), None, ("test period",)),
        (_measured(0.86, holdout=0.70), None, ("held-out",)),
        (_measured(0.86, latency=51.0), None, ("latency",)),
        (_measured(0.86), 0.87, ("champion",)),
        (_measured(0.87), 0.87, ()),  # equal to the champion is not a regression
        (_measured(0.5, latency=99.0), 0.9, ("test period", "latency", "champion")),
    ],
)
def test_gate_lists_every_failed_condition(
    measured: p.Measured, champion: float | None, expected: tuple[str, ...]
) -> None:
    reasons = p.gate_reasons(measured, champion, p.MetricsGate())
    assert len(reasons) == len(expected)
    for reason, word in zip(reasons, expected, strict=True):
        assert word in reason


# --------------------------------------------------------------------------- promote (real runs)
def test_good_run_is_measured_registered_and_becomes_champion(registry: Registry) -> None:
    result = _promote(registry, registry.good_run, "promo-good")
    assert result.promoted and result.version == "1" and result.champion_version == "1"
    # The gate's number is the evaluation report's number for the served model, recomputed here.
    test = t.time_split(load_export(registry.data)).test
    expected = served_metrics(inf.load(registry.good_dir), test).pr_auc
    assert result.gate.candidate.pr_auc_test == expected >= GATE.min_pr_auc
    assert 0 < result.gate.candidate.latency_p95_ms <= GATE.max_latency_p95_ms
    client = _client(registry)
    assert _aliases(registry, "promo-good") == {"champion": "1"}
    tags = client.get_model_version("promo-good", "1").tags
    assert tags["gate"] == "passed"
    assert json.loads(tags["measured"])["pr_auc_test"] == expected
    manifest = (registry.good_dir / inf.MANIFEST).read_bytes()
    assert tags["manifest_sha256"] == hashlib.sha256(manifest).hexdigest()


def test_bad_run_fails_the_real_evaluation_although_its_report_claims_099(
    registry: Registry,
) -> None:
    result = _promote(registry, registry.bad_run, "promo-bad")
    assert not result.promoted and result.champion_version is None
    assert result.gate.candidate.pr_auc_test < GATE.min_pr_auc  # measured, not the claimed 0.99
    assert any("test period" in reason for reason in result.gate.reasons)
    client = _client(registry)
    assert _aliases(registry, "promo-bad") == {"challenger": result.version}
    assert client.get_model_version("promo-bad", result.version).tags["gate"] == "failed"


def test_challenger_worse_than_the_champion_is_rejected_and_champion_stays(
    registry: Registry,
) -> None:
    assert _promote(registry, registry.good_run, "promo-cc").promoted
    lenient = p.MetricsGate(min_pr_auc=0.0, max_latency_p95_ms=1_000.0)  # only the champion test
    result = _promote(registry, registry.bad_run, "promo-cc", lenient)
    assert not result.promoted and result.version == "2"
    assert result.gate.champion_version == "1"
    assert result.gate.champion_pr_auc_test is not None
    assert result.gate.candidate.pr_auc_test < result.gate.champion_pr_auc_test
    assert any("champion" in reason for reason in result.gate.reasons)
    assert _aliases(registry, "promo-cc") == {"champion": "1", "challenger": "2"}
    # An equally good challenger (same rows, same PR-AUC) replaces the champion.
    again = _promote(registry, registry.good_run, "promo-cc")
    assert again.promoted and again.champion_version == "3"
    assert _aliases(registry, "promo-cc") == {"champion": "3"}


def test_latency_gate_uses_the_measured_latency(registry: Registry) -> None:
    impossible = p.MetricsGate(min_pr_auc=0.0, max_latency_p95_ms=0.001)
    result = _promote(registry, registry.good_run, "promo-slow", impossible)
    assert not result.promoted and result.gate.candidate.latency_p95_ms > 0.001
    assert len(result.gate.reasons) == 1 and "latency" in result.gate.reasons[0]


def test_evaluation_data_must_be_the_export_the_run_was_trained_on(
    registry: Registry, tmp_path: Path
) -> None:
    _export_fraud(GeneratorConfig(seed=7, customers=150, months=2), tmp_path)
    with pytest.raises(p.PromotionError, match="not the export"):
        p.promote(registry.uri, registry.good_run, tmp_path, model_name="promo-data")
    assert _client(registry).search_model_versions("name='promo-data'") == []


def test_run_without_training_artifacts_is_refused(registry: Registry) -> None:
    client = _client(registry)
    experiment = client.get_experiment_by_name("pbip-fraud")
    assert experiment is not None
    empty = client.create_run(experiment.experiment_id)
    client.set_terminated(empty.info.run_id)
    with pytest.raises(p.PromotionError, match=r"has no lightgbm\.txt"):
        p.promote(registry.uri, empty.info.run_id, registry.data, model_name="promo-empty")


# --------------------------------------------------------------------------- export
def test_exported_champion_passes_the_serving_loader_and_replaces_old_files(
    registry: Registry, tmp_path: Path
) -> None:
    promoted = _promote(registry, registry.good_run, "promo-export")
    served = tmp_path / "models" / "fraud"
    served.mkdir(parents=True)
    (served / "stale.txt").write_text("from an older model", encoding="utf-8")
    exported = p.export_champion(registry.uri, served, "promo-export")
    assert exported.version == promoted.version and exported.run_id == registry.good_run
    expected_files = {*inf.ARTIFACTS, inf.MANIFEST, "report.json", "champion.json"}
    assert set(exported.files) == expected_files  # stale.txt went with the old directory
    scorer = inf.load(served)  # the API's startup loader, checksums included
    assert scorer.params.model_version == "hybrid-lgbm-iforest-1"
    for name in (*inf.ARTIFACTS, inf.MANIFEST):
        assert (served / name).read_bytes() == (registry.good_dir / name).read_bytes()
    provenance = json.loads((served / "champion.json").read_text(encoding="utf-8"))
    assert provenance["version"] == promoted.version and provenance["alias"] == "champion"
    assert [f.name for f in served.parent.iterdir()] == ["fraud"]  # no staging directory left


def _copy(source: Path, target: Path) -> dict[str, bytes]:
    target.mkdir(parents=True)
    for f in source.iterdir():
        shutil.copy(f, target / f.name)
    return {f.name: f.read_bytes() for f in target.iterdir()}


def test_export_with_mlflow_unreachable_keeps_serving_the_files_on_disk(
    registry: Registry, tmp_path: Path
) -> None:
    served = tmp_path / "fraud"
    before = _copy(registry.good_dir, served)
    with pytest.raises(p.PromotionError, match="unreachable"):
        p.export_champion("http://127.0.0.1:1", served)
    assert {f.name: f.read_bytes() for f in served.iterdir()} == before
    assert inf.load(served).params.model_version == "hybrid-lgbm-iforest-1"


def test_export_refuses_artifacts_that_differ_from_what_the_gate_measured(
    registry: Registry, tmp_path: Path
) -> None:
    _promote(registry, registry.good_run, "promo-tamper")
    served = tmp_path / "fraud"
    before = _copy(registry.good_dir, served)
    client = _client(registry)
    client.set_model_version_tag("promo-tamper", "1", "manifest_sha256", "0" * 64)
    with pytest.raises(p.PromotionError, match="differ from the ones the gate measured"):
        p.export_champion(registry.uri, served, "promo-tamper")
    assert {f.name: f.read_bytes() for f in served.iterdir()} == before
    assert [f.name for f in tmp_path.iterdir()] == ["fraud"]  # staging cleaned up


def test_export_refuses_a_champion_that_did_not_pass_the_gate(registry: Registry) -> None:
    rejected = _promote(registry, registry.bad_run, "promo-forced")
    client = _client(registry)
    client.set_registered_model_alias("promo-forced", "champion", rejected.version)  # by hand
    with pytest.raises(p.PromotionError, match="did not pass the gate"):
        p.export_champion(registry.uri, Path("unused"), "promo-forced")
    with pytest.raises(p.PromotionError, match="has no champion alias"):
        p.export_champion(registry.uri, Path("unused"), "no-such-model")


# --------------------------------------------------------------------------- CLI and inputs
def test_cli_promotes_exports_and_reports_rejections_by_exit_code(
    registry: Registry, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = ["--tracking-uri", registry.uri, "--model-name", "promo-cli"]
    gate = ["--min-pr-auc", "0.6", "--max-latency-ms", "1000", "--latency-calls", "20"]
    data = ["--data", str(registry.data)]
    assert p.main(["promote", *common, "--run-id", registry.good_run, *data, *gate]) == 0
    assert json.loads(capsys.readouterr().out)["champion_version"] == "1"
    assert p.main(["promote", *common, "--run-id", registry.bad_run, *data, *gate]) == 1
    assert json.loads(capsys.readouterr().out)["promoted"] is False
    out = tmp_path / "served"
    assert p.main(["export-champion", *common, "--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == "1"
    assert inf.load(out).params.model_version == "hybrid-lgbm-iforest-1"


def test_latest_run_is_the_newest_finished_training_run(registry: Registry) -> None:
    client = _client(registry)
    experiment = client.get_experiment_by_name("pbip-fraud")
    assert experiment is not None
    finished = client.search_runs(
        [experiment.experiment_id], "attributes.status = 'FINISHED'",
        order_by=["attributes.start_time DESC"],
    )  # fmt: skip
    assert p.latest_training_run(client) == finished[0].info.run_id
    assert registry.bad_run in {r.info.run_id for r in finished}
    with pytest.raises(p.PromotionError, match="not found"):
        p.latest_training_run(client, "no-such-experiment")


@pytest.mark.parametrize(
    "argv",
    [
        ["export-champion", "--tracking-uri", "file:///etc", "--out", "x"],
        ["export-champion", "--tracking-uri", "http://127.0.0.1:1", "--out", "x"],
        [
            "export-champion",
            "--tracking-uri",
            "sqlite:///x.db",
            "--model-name",
            "../x",
            "--out",
            "x",
        ],
    ],
)
def test_cli_refuses_bad_uris_names_and_unreachable_servers(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert p.main(argv) == 2
    assert "promotion failed" in capsys.readouterr().err


def test_tracking_uri_comes_from_argument_then_environment_never_a_local_fallback() -> None:
    environ = {TRACKING_URI_ENV: "http://x:5000"}
    assert p.tracking_uri_from("sqlite:///a.db", environ) == "sqlite:///a.db"
    assert p.tracking_uri_from(None, {TRACKING_URI_ENV: "https://mlflow"}) == "https://mlflow"
    assert p.tracking_uri_from(None, {}) == "http://localhost:5000"
    with pytest.raises(p.PromotionError):
        p.tracking_uri_from("s3://bucket/x", {})
