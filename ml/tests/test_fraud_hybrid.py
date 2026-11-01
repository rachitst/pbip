"""TASK-031/032: hybrid combiner, thresholds, fail-safe, overrides and SHAP reasons per scenario."""

import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pbip_datagen.__main__ import _export_fraud
from pbip_datagen.generator import GeneratorConfig
from pbip_ml.fraud import hybrid as h
from pbip_ml.fraud import training as t
from pbip_ml.fraud.contract import FraudDecision, ReasonSource
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
    params = h.fit_params(split.validation, booster, anomaly, rules, "hybrid-test")
    return {
        "split": split,
        "data": data,
        "scorer": h.HybridScorer(params, booster, anomaly, rules),
        "params": params,
        "booster": booster,
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
    timings = []
    for i in range(min(100, len(fraud))):
        started = time.process_time()  # CPU time: robust to other test workers
        h.shap_reasons(booster, fraud.iloc[[i]])
        timings.append((time.process_time() - started) * 1000)
    timings.sort()
    assert timings[int(len(timings) * 0.95) - 1] < 30


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
    import lightgbm as lgb

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
