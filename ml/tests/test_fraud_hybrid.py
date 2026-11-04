"""TASK-031/032: hybrid combiner, thresholds, fail-safe, overrides and SHAP reasons per scenario."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import lightgbm as lgb
import numpy as np
import pandas as pd
import pytest
from pbip_datagen.__main__ import _export_fraud
from pbip_datagen.generator import GeneratorConfig
from pbip_ml.fraud import hybrid as h
from pbip_ml.fraud import training as t
from pbip_ml.fraud.artifacts import ANOMALY_FEATURES
from pbip_ml.fraud.contract import FraudDecision, ReasonSource
from pbip_ml.fraud.features import PastTransfer
from pbip_ml.fraud.inference import TransferInputs, score_transfer
from pbip_ml.fraud.train import load_export


@pytest.fixture(scope="module")
def trained(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    out = tmp_path_factory.mktemp("hybrid")
    _export_fraud(GeneratorConfig(seed=42, customers=1500, months=6), out)
    data = load_export(out)
    labels = pd.read_csv(out / "fraud_labels.csv")
    transfers = pd.read_csv(out / "transfers.csv")
    data["scenario"] = transfers["id"].map(
        labels[labels["fraudulent"].astype(str) == "True"].set_index("transaction_id")["scenario"]
    )
    split = t.time_split(data)
    booster, _ = t.train_lightgbm(split)
    anomaly = t.train_isolation_forest(split)
    rules = h.default_registry()
    out_of_time = t.out_of_time_scores(split, booster.best_iteration)
    params = h.fit_params(split.validation, booster, anomaly, rules, "hybrid-test", out_of_time)
    return {
        "split": split,
        "data": data,
        "scorer": h.HybridScorer(params, booster, anomaly, rules),
        "params": params,
        "booster": booster,
        "anomaly": anomaly,
        "out_of_time": out_of_time,
    }


def _score(scorer: h.HybridScorer, frame: pd.DataFrame, i: int = 0) -> h.HybridResult:
    return scorer.score(frame.iloc[[i]], h.rule_context(frame.iloc[i]))


@pytest.mark.parametrize(
    ("risk", "decision"),
    [(0, "ALLOW"), (29, "ALLOW"), (30, "STEP_UP"), (59, "STEP_UP"), (60, "REVIEW"),
     (84, "REVIEW"), (85, "BLOCK"), (100, "BLOCK")],
)  # fmt: skip
def test_risk_maps_to_actions_at_the_documented_thresholds(risk: int, decision: str) -> None:
    assert h.decide(risk) is FraudDecision(decision)


# #362: scoring ~1,700 rows one by one (plus training, the first test to use the fixture) takes
# about 45 s on an idle 2-CPU laptop, near the 60 s default hang guard, so a busy full run timed
# out. The gate is about behaviour, not speed; this limit only guards against a hang.
@pytest.mark.timeout(300)
def test_hybrid_flags_fraud_and_passes_normal_traffic(trained: dict[str, object]) -> None:
    split, scorer = trained["split"], trained["scorer"]
    assert isinstance(split, t.Split) and isinstance(scorer, h.HybridScorer)
    # every fraud row plus a fixed sample of normal rows (row-by-row scoring is ~20 ms each)
    test = pd.concat(
        [split.test[split.test["label"] == 1],
         split.test[split.test["label"] == 0].sample(1500, random_state=7)]
    )  # fmt: skip
    results = h.score_frame(scorer, test)
    risk = np.array([r.risk_score for r in results]) / 100
    at_review = t.evaluate(test["label"].to_numpy(), risk, h.REVIEW_AT / 100)
    assert at_review.recall >= 0.8 and at_review.fpr <= 0.01  # behaviour gate on the test period
    assert all(0 <= r.risk_score <= 100 and r.model_version == "hybrid-test" for r in results)


def test_fails_safe_to_rules_only_never_allow_all(trained: dict[str, object]) -> None:
    split, scorer = trained["split"], trained["scorer"]
    assert isinstance(split, t.Split) and isinstance(scorer, h.HybridScorer)
    rules_only = replace(scorer, booster=None, anomaly=None)
    fraud = split.test[split.test["label"] == 1]
    results = h.score_frame(rules_only, fraud)
    assert all(r.model_version == "hybrid-test+rules-only" for r in results)
    assert all(any(x.code == "MODEL_UNAVAILABLE" for x in r.reasons) for r in results)
    assert sum(r.decision is not FraudDecision.ALLOW for r in results) >= len(results) // 2
    for r, (_, row) in zip(results, fraud.iterrows(), strict=True):
        assert r.risk_score >= h.default_registry().evaluate(h.rule_context(row)).score


def test_a_model_that_raises_also_falls_back(trained: dict[str, object]) -> None:
    split, scorer = trained["split"], trained["scorer"]
    assert isinstance(split, t.Split) and isinstance(scorer, h.HybridScorer)

    class Broken:
        def feature_name(self) -> list[str]:  # the booster interface the scorer reads
            return list(t.MODEL_FEATURES)

        def predict(self, *_a: object, **_k: object) -> object:
            raise ValueError("corrupt model")

    result = _score(replace(scorer, booster=Broken()), split.test)
    assert result.model_version.endswith("+rules-only")
    assert result.features["model_error"] == "ValueError"


def test_hard_rule_overrides_raise_the_floor(trained: dict[str, object]) -> None:
    split, scorer = trained["split"], trained["scorer"]
    assert isinstance(split, t.Split) and isinstance(scorer, h.HybridScorer)
    quiet = split.test[split.test["label"] == 0].iloc[[0]].copy()
    takeover = quiet.assign(failure_count_24h=5, new_device=1)
    result = _score(scorer, takeover)
    assert result.risk_score >= h.REVIEW_AT
    assert any(r.code == "RAPID_ATO_TRANSFER" for r in result.reasons)


def test_params_round_trip_as_json(trained: dict[str, object]) -> None:
    params = trained["params"]
    assert isinstance(params, h.HybridParams)
    assert h.HybridParams.from_json(params.to_json()) == params


def test_shap_reasons_are_top3_positive_exact_and_fast(trained: dict[str, object]) -> None:
    split, booster = trained["split"], trained["booster"]
    assert isinstance(split, t.Split)
    fraud = split.test[split.test["label"] == 1]
    row = fraud.iloc[[0]]
    reasons = h.shap_reasons(booster, row)
    assert 1 <= len(reasons) <= 3
    assert all(r.source is ReasonSource.MODEL and (r.contribution or 0) > 0 for r in reasons)
    contributions = [r.contribution or 0.0 for r in reasons]
    assert contributions == sorted(contributions, reverse=True)
    contrib = np.asarray(booster.predict(row[list(t.MODEL_FEATURES)], pred_contrib=True))[0]
    raw = np.asarray(booster.predict(row[list(t.MODEL_FEATURES)], raw_score=True))[0]
    assert contrib.sum() == pytest.approx(raw, abs=1e-9)  # exact TreeSHAP decomposition
    # Cost, not wall time (#317: CPU time sums OpenMP threads and ticks in 15.6 ms on Windows):
    # one reason set is ONE native TreeSHAP call on ONE row, never a sampling explainer or a
    # per-feature loop. Real latency is measured by scripts/benchmark_fraud_latency.py.
    calls: list[tuple[int, dict[str, object]]] = []

    class CountingBooster:
        def feature_name(self) -> list[str]:  # metadata only, not a prediction
            return list(booster.feature_name())

        def predict(self, data: pd.DataFrame, **kwargs: object) -> object:
            calls.append((len(data), kwargs))
            return booster.predict(data, **kwargs)

    for i in range(min(100, len(fraud))):
        calls.clear()
        h.shap_reasons(CountingBooster(), fraud.iloc[[i]])  # type: ignore[arg-type]  # spy
        assert calls == [(1, {"pred_contrib": True, "num_threads": 1})]  # #379: no OpenMP pool


@pytest.mark.parametrize("scenario", ["F1", "F2", "F3", "F4", "F5", "F6"])
def test_every_scenario_gets_unified_readable_reasons(
    trained: dict[str, object], scenario: str
) -> None:
    data, scorer = trained["data"], trained["scorer"]
    assert isinstance(data, pd.DataFrame) and isinstance(scorer, h.HybridScorer)
    rows = data[data["scenario"] == scenario]
    assert len(rows) > 0
    # F2: the first leg of a burst has no earlier transfers yet; explain a leg inside the burst.
    index = int(np.argmax(rows["velocity_10m"].to_numpy())) if scenario == "F2" else 0
    result = _score(scorer, rows, index)
    assert result.reasons, scenario
    for reason in result.reasons:
        assert reason.message and "nan" not in reason.message.lower()
        assert reason.source in (ReasonSource.RULE, ReasonSource.MODEL, ReasonSource.ANOMALY)
    expected_words = {
        "F1": ("payee",),
        "F2": ("transfers",),
        "F3": ("sign-in", "device"),
        "F5": ("average", "amount"),
        "F6": ("amount", "payee", "transfers", "device", "money"),
    }.get(scenario)
    if expected_words and result.risk_score >= h.STEP_UP_AT:
        text = " ".join(r.message.lower() for r in result.reasons)
        assert any(word in text for word in expected_words), (scenario, text)


def test_feature_snapshot_is_recorded_for_audit(trained: dict[str, object]) -> None:
    split, scorer = trained["split"], trained["scorer"]
    assert isinstance(split, t.Split) and isinstance(scorer, h.HybridScorer)
    result = _score(scorer, split.test)
    assert set(t.MODEL_FEATURES) <= set(result.features)
    assert {"rule_points", "model_probability", "anomaly_score"} <= set(result.features)


def test_artifacts_load_from_disk(trained: dict[str, object], tmp_path: Path) -> None:
    booster, params = trained["booster"], trained["params"]
    assert isinstance(params, h.HybridParams)
    booster.save_model(str(tmp_path / "m.txt"))
    loaded = lgb.Booster(model_file=str(tmp_path / "m.txt"))
    split = trained["split"]
    assert isinstance(split, t.Split)
    x = split.test[list(t.MODEL_FEATURES)].iloc[:50]
    assert np.allclose(loaded.predict(x), booster.predict(x))


def test_batch_risk_matches_row_scoring(trained: dict[str, object]) -> None:
    split, scorer = trained["split"], trained["scorer"]
    assert isinstance(split, t.Split) and isinstance(scorer, h.HybridScorer)
    sample = pd.concat([split.test[split.test["label"] == 1].head(40), split.test.head(160)])
    for candidate in (scorer, replace(scorer, booster=None, anomaly=None)):
        rows = [r.risk_score for r in h.score_frame(candidate, sample)]
        assert h.batch_risk(candidate, sample).tolist() == rows


def _app_scam() -> TransferInputs:
    """The canonical APP scam of ``test_fraud_behaviour_regression``: a settled customer pays
    INR 45,000 (22x the usual) to a payee added 15 minutes earlier, from the usual phone."""
    now = datetime(2026, 6, 15, 13, 0, tzinfo=UTC)
    phone = UUID("00000000-0000-4000-8000-000000000001")
    history = tuple(
        PastTransfer(now - timedelta(days=i, minutes=3), "DEBIT", 2_000_00, phone)
        for i in range(1, 21)
    )
    return TransferInputs(
        45_000_00, "INR", now, now - timedelta(minutes=15), phone, False, 0, history
    )


def test_half_size_model_still_holds_the_known_app_scam(trained: dict[str, object]) -> None:
    """#396 regression. Calibrated on its 42 validation positives alone, this 1,500-customer model
    mapped the scam's raw 0.90 onto a 0.40 isotonic step: risk 30, STEP_UP. Calibrated on the
    out-of-time training scores as well (ADR-0047), it is held for review at least."""
    scorer = trained["scorer"]
    assert isinstance(scorer, h.HybridScorer)
    assert score_transfer(scorer, _app_scam()).risk_score >= h.REVIEW_AT


def test_calibration_rests_on_more_positives_and_is_finer(trained: dict[str, object]) -> None:
    """#396: the out-of-time scores add positives, so the isotonic table has more steps than the
    validation-only fit, and it never maps a higher raw score to a lower probability."""
    split, booster, anomaly = trained["split"], trained["booster"], trained["anomaly"]
    params, out_of_time = trained["params"], trained["out_of_time"]
    assert isinstance(split, t.Split) and isinstance(params, h.HybridParams)
    assert isinstance(anomaly, t.AnomalyModel) and isinstance(booster, lgb.Booster)
    assert isinstance(out_of_time, tuple)
    validation_only = h.fit_params(split.validation, booster, anomaly, h.default_registry(), "v")
    assert out_of_time[1].sum() > split.validation["label"].sum()
    assert len(params.calibration_x) > len(validation_only.calibration_x)
    assert params.calibration_y == sorted(params.calibration_y)


LEGACY_FEATURES = [
    name
    for name in t.MODEL_FEATURES
    if name not in ("near_threshold_72h", "days_since_last_activity")
]


@pytest.mark.timeout(300)  # may pay for the module's training fixture (#362)
def test_a_model_trained_before_the_new_features_still_scores(trained: dict[str, object]) -> None:
    """ADR-0051: the -3 artifacts (9 features) are served by code whose row has 11 columns.

    LightGBM matches columns by position, so the row is cut to the booster's own feature list. A
    9-feature model must score with the model, not fall back to the rules, and give exactly what it
    gives on its own columns.
    """
    split, params, anomaly = trained["split"], trained["params"], trained["anomaly"]
    assert isinstance(split, t.Split) and isinstance(params, h.HybridParams)
    assert isinstance(anomaly, t.AnomalyModel)
    legacy = lgb.train(
        t.LGBM_PARAMS, lgb.Dataset(split.train[LEGACY_FEATURES], split.train["label"]), 20
    )
    assert h.model_columns(legacy) == LEGACY_FEATURES
    scorer = h.HybridScorer(params, legacy, anomaly)
    frame = pd.concat([split.test[split.test["label"] == 1].head(5), split.test.head(5)])
    assert set(t.MODEL_FEATURES) <= set(frame.columns)  # the new, wider row
    for i in range(len(frame)):
        result = _score(scorer, frame, i)
        assert result.model_version == params.model_version  # not "+rules-only"
        expected = float(
            params.calibrate(np.asarray(legacy.predict(frame.iloc[[i]][LEGACY_FEATURES])))[0]
        )
        assert result.features["model_probability"] == round(expected, 4)
        assert all(r.code.removeprefix("MODEL_").lower() in LEGACY_FEATURES
                   for r in result.reasons if r.source is ReasonSource.MODEL)  # fmt: skip
    assert len(h.batch_risk(scorer, frame)) == len(frame)


@pytest.mark.timeout(300)  # may pay for the module's training fixture (#362)
def test_legacy_artifacts_on_disk_load_and_are_served_by_the_model(
    trained: dict[str, object], tmp_path: Path
) -> None:
    """ADR-0051, the deploy order: the API loads the -3 artifacts (9 features) with this code.

    The artifacts are written exactly as ``train.py`` writes them (LightGBM text model, joblib
    forest, params JSON, manifest) and read through ``inference.load``, the API's loader. The
    served row then has 11 columns; the score must come from the model, never "+rules-only".
    """
    import joblib
    from pbip_ml.fraud.inference import load, write_manifest

    split, params, anomaly = trained["split"], trained["params"], trained["anomaly"]
    assert isinstance(split, t.Split) and isinstance(params, h.HybridParams)
    legacy = lgb.train(
        t.LGBM_PARAMS, lgb.Dataset(split.train[LEGACY_FEATURES], split.train["label"]), 20
    )
    legacy.save_model(str(tmp_path / "lightgbm.txt"))
    joblib.dump(anomaly, tmp_path / "isolation_forest.joblib")
    old = replace(params, model_version="hybrid-lgbm-iforest-3")
    (tmp_path / "hybrid_params.json").write_text(old.to_json(), encoding="utf-8")
    write_manifest(tmp_path)
    served = load(tmp_path)
    assert h.model_columns(served.booster) == LEGACY_FEATURES  # type: ignore[arg-type]  # loaded
    now = datetime(2026, 3, 2, 12, tzinfo=UTC)
    history = [PastTransfer(now - timedelta(days=i), "DEBIT", 2_000_00, None) for i in range(1, 9)]
    inputs = TransferInputs(9_00_000_00 // 10, "INR", now, now - timedelta(minutes=5), None, True,
                            0, history)  # fmt: skip
    result = score_transfer(served, inputs)
    assert result.model_version == "hybrid-lgbm-iforest-3"  # the model scored, not the rules alone
    assert "model_probability" in result.features and "model_error" not in result.features


# #379: the LightGBM step was made cheaper (one float64 matrix shared by predict and TreeSHAP, one
# thread). Neither may change a decision, so these tests demand exact equality with the previous
# arithmetic (DataFrame predict), not closeness.
def _reference(
    scorer: h.HybridScorer, frame: pd.DataFrame, i: int
) -> tuple[int, float, float, list[float]]:
    """The scoring arithmetic as it was before #379: DataFrame predict and ``score_samples``."""
    assert scorer.booster is not None and scorer.anomaly is not None
    row, outcome = frame.iloc[[i]], scorer.rules.evaluate(h.rule_context(frame.iloc[i]))
    raw = float(np.asarray(scorer.booster.predict(row[list(t.MODEL_FEATURES)]))[0])
    p = float(scorer.params.calibrate(np.array([raw]))[0])
    model = scorer.anomaly
    forest_raw = -model.forest.score_samples(row[list(ANOMALY_FEATURES)].to_numpy(dtype=float))
    a = float(np.clip((forest_raw - model.low) / (model.high - model.low), 0.0, 1.0)[0])
    z = (
        scorer.params.intercept
        + scorer.params.w_model * p
        + scorer.params.w_anomaly * a
        + scorer.params.w_rules * outcome.score / 100
    )
    risk = round(100 * h._sigmoid(z))
    for hit in outcome.triggered:
        risk = max(risk, h.OVERRIDES.get(hit.code, 0))
    contrib = np.asarray(scorer.booster.predict(row[list(t.MODEL_FEATURES)], pred_contrib=True))
    return min(max(risk, 0), 100), p, a, [float(v) for v in contrib[0]]


@pytest.mark.timeout(300)
def test_cheaper_scoring_gives_exactly_the_previous_scores_and_reasons(
    trained: dict[str, object],
) -> None:
    split, scorer = trained["split"], trained["scorer"]
    assert isinstance(split, t.Split) and isinstance(scorer, h.HybridScorer)
    assert scorer.booster is not None
    sample = pd.concat(
        [split.test[split.test["label"] == 1],
         split.test[split.test["label"] == 0].sample(300, random_state=11)]
    )  # fmt: skip
    for i in range(len(sample)):
        result = _score(scorer, sample, i)
        risk, p, a, contrib = _reference(scorer, sample, i)
        assert result.risk_score == risk  # exact: the same integer, so the same decision
        assert result.features["model_probability"] == round(p, 4)
        assert result.features["anomaly_score"] == round(a, 4)
        model_reasons = [r for r in result.reasons if r.source is ReasonSource.MODEL]
        values = np.asarray(contrib[:-1])
        expected = [
            (f"MODEL_{t.MODEL_FEATURES[j].upper()}", round(float(values[j]), 4))
            for j in np.argsort(-values)[: h.TOP_SHAP]
            if values[j] > 0
        ]
        assert [(r.code, r.contribution) for r in model_reasons] == expected


def test_one_score_is_one_single_threaded_predict_and_one_treeshap_call(
    trained: dict[str, object],
) -> None:
    split, scorer = trained["split"], trained["scorer"]
    assert isinstance(split, t.Split) and isinstance(scorer, h.HybridScorer)
    assert scorer.booster is not None
    booster: lgb.Booster = scorer.booster
    calls: list[tuple[type, tuple[int, ...], dict[str, Any]]] = []

    class CountingBooster:
        def feature_name(self) -> list[str]:  # metadata only, not a prediction
            return list(booster.feature_name())

        def predict(self, data: np.ndarray, **kwargs: Any) -> object:
            calls.append((type(data), data.shape, kwargs))
            return booster.predict(data, **kwargs)

    spied = replace(scorer, booster=CountingBooster())  # type: ignore[arg-type]  # spy
    result = _score(spied, split.test[split.test["label"] == 1])
    assert result.model_version == "hybrid-test"
    shape = (1, len(t.MODEL_FEATURES))
    assert calls == [
        (np.ndarray, shape, {"num_threads": 1}),
        (np.ndarray, shape, {"pred_contrib": True, "num_threads": 1}),
    ]
