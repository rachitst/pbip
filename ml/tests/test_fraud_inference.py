"""TASK-033: the serving module - artifacts load safely, and the served row equals the training row."""

import json
import pickle
from pathlib import Path
from uuid import UUID

import joblib
import pandas as pd
import pytest
from pbip_datagen.__main__ import _export_fraud
from pbip_datagen.fraud import Transfer, generate_labelled
from pbip_datagen.generator import GeneratorConfig
from pbip_ml.fraud import inference as inf
from pbip_ml.fraud import training as t
from pbip_ml.fraud.artifacts import AnomalyModel, load_anomaly_model
from pbip_ml.fraud.features import PastTransfer
from pbip_ml.fraud.train import load_export, run


@pytest.fixture(scope="module")
def artifacts(tmp_path_factory: pytest.TempPathFactory) -> Path:
    data, out = tmp_path_factory.mktemp("data"), tmp_path_factory.mktemp("model")
    _export_fraud(GeneratorConfig(seed=42, customers=600, months=4), data)
    run(data, out)
    return out


def test_training_writes_a_manifest_that_load_verifies(artifacts: Path) -> None:
    manifest = json.loads((artifacts / inf.MANIFEST).read_text(encoding="utf-8"))
    assert set(manifest) == set(inf.ARTIFACTS) and all(len(v) == 64 for v in manifest.values())
    scorer = inf.load(artifacts)
    assert scorer.booster is not None and scorer.anomaly is not None
    assert scorer.params.model_version == "hybrid-lgbm-iforest-1"


@pytest.mark.parametrize("damage", ["edit_params", "drop_manifest", "drop_model", "foreign_pickle"])
def test_damaged_artifacts_are_refused(artifacts: Path, tmp_path: Path, damage: str) -> None:
    for f in artifacts.iterdir():
        (tmp_path / f.name).write_bytes(f.read_bytes())
    if damage == "edit_params":
        (tmp_path / "hybrid_params.json").write_text('{"model_version": "x"}', encoding="utf-8")
    elif damage == "drop_manifest":
        (tmp_path / inf.MANIFEST).unlink()
    elif damage == "drop_model":
        (tmp_path / "lightgbm.txt").unlink()
    else:  # a valid pickle of the wrong type, with a matching manifest: still refused
        import joblib

        joblib.dump({"not": "a model"}, tmp_path / "isolation_forest.joblib")
        inf.write_manifest(tmp_path)
    with pytest.raises(inf.ModelLoadError):
        inf.load(tmp_path)


def _inputs(transfer: Transfer, history: list[Transfer]) -> inf.TransferInputs:
    return inf.TransferInputs(
        amount_minor=transfer.amount_minor,
        currency="INR",
        occurred_at=transfer.occurred_at,
        payee_added_at=transfer.payee_added_at,
        device_id=transfer.device_id,
        new_device=transfer.new_device,
        failed_logins_24h=transfer.failed_logins_24h,
        history=[
            PastTransfer(h.occurred_at, str(h.direction), h.amount_minor, h.device_id)  # type: ignore[arg-type]  # generator emits DEBIT/CREDIT
            for h in history
        ],
    )


def test_served_row_equals_the_training_row(tmp_path: Path) -> None:
    """Every model column of every debit, built online, matches ``build_dataset`` (training)."""
    config = GeneratorConfig(seed=11, customers=120, months=3)
    _export_fraud(config, tmp_path)
    offline = load_export(tmp_path)  # outgoing rows, indexed by position in transfers.csv
    transfers = generate_labelled(config).transfers
    by_customer: dict[UUID, list[Transfer]] = {}
    for transfer in transfers:
        by_customer.setdefault(transfer.customer_id, []).append(transfer)
    checked = 0
    for i, transfer in enumerate(transfers):
        if str(transfer.direction) != "DEBIT":
            continue
        history = [h for h in by_customer[transfer.customer_id] if h.id != transfer.id]
        row, context = inf.feature_row(_inputs(transfer, history))
        expected = offline.loc[i]
        for name in t.MODEL_FEATURES:
            assert row.iloc[0][name] == pytest.approx(expected[name], rel=0, abs=1e-12), name
        assert context.new_device == transfer.new_device
        checked += 1
    assert checked > 300


def test_loaded_scorer_scores_exactly_like_the_in_memory_one(artifacts: Path) -> None:
    scorer = inf.load(artifacts)
    again = inf.load(artifacts)
    data = generate_labelled(GeneratorConfig(seed=5, customers=40, months=2)).transfers
    debit = next(x for x in data if str(x.direction) == "DEBIT")
    inputs = _inputs(debit, [])
    first, second = inf.score_transfer(scorer, inputs), inf.score_transfer(again, inputs)
    assert (first.risk_score, first.decision, first.model_version) == (
        second.risk_score,
        second.decision,
        second.model_version,
    )
    assert not first.model_version.endswith("+rules-only")
    anomaly = first.features["anomaly_score"]
    assert isinstance(anomaly, float) and 0.0 <= anomaly <= 1.0


def _copy(source: Path, target: Path) -> Path:
    target.mkdir()
    for f in source.iterdir():
        (target / f.name).write_bytes(f.read_bytes())
    return target


def test_an_artifact_pickled_under_the_pre_adr_0033_class_path_still_loads(
    artifacts: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#350: artifacts written before ADR-0033 name ``pbip_ml.fraud.training.AnomalyModel``. The
    allow-listed unpickler maps that path to the inference-safe class, and the scores are equal."""
    current = inf.load(artifacts)
    legacy = _copy(artifacts, tmp_path / "legacy")
    monkeypatch.setattr(AnomalyModel, "__module__", "pbip_ml.fraud.training")
    joblib.dump(current.anomaly, legacy / "isolation_forest.joblib")
    monkeypatch.undo()
    inf.write_manifest(legacy)
    payload = (legacy / "isolation_forest.joblib").read_bytes()
    assert b"pbip_ml.fraud.training" in payload and b"pbip_ml.fraud.artifacts" not in payload

    loaded = inf.load(legacy)

    assert type(loaded.anomaly) is AnomalyModel and current.anomaly is not None
    frame = pd.DataFrame(
        [
            {"ratio_to_avg_amount": r, "velocity_10m": v, "distinct_devices_7d": 1, "log_amount": a}
            for r, v, a in ((1.0, 0, 3.0), (25.0, 6, 6.5), (0.2, 1, 2.0))
        ]
    )
    assert list(loaded.anomaly.score(frame)) == list(current.anomaly.score(frame))


class _OpensAFile:
    """A pickle that would create ``path`` when loaded: the classic code-execution payload."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __reduce__(self) -> tuple[object, tuple[str, str]]:
        return open, (str(self.path), "w")


def test_a_pickle_that_names_a_class_outside_the_allow_list_is_refused_before_it_runs(
    artifacts: Path, tmp_path: Path
) -> None:
    """T34 hardening (ADR-0033): even with a matching manifest, a pickle that calls anything but
    the Isolation Forest classes is refused, and the call never happens."""
    marker = tmp_path / "ran"
    tampered = _copy(artifacts, tmp_path / "tampered")
    joblib.dump(_OpensAFile(marker), tampered / "isolation_forest.joblib")
    inf.write_manifest(tampered)

    with pytest.raises(pickle.UnpicklingError, match=r"\.open is not allowed"):
        load_anomaly_model(tampered / "isolation_forest.joblib")
    with pytest.raises(inf.ModelLoadError):
        inf.load(tampered)
    assert not marker.exists()
