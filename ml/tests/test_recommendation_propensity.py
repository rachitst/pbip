"""The simulated-uptake propensity model (TASK-054, ADR-0049): the simulation is deterministic and
leaks no future data, the model only re-ranks what the rules allow, the artefact loads only when
intact and promoted, and everything it produces says it was trained on simulated uptake."""

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import numpy as np
import pytest
from pbip_ml.recommendations import training
from pbip_ml.recommendations.contract import (
    CustomerRecommendationContext,
    HeldProduct,
    HeldProductKind,
)
from pbip_ml.recommendations.engine import (
    FIXED_DEPOSIT_ID,
    INDEX_SIP_ID,
    PRODUCT_CATALOGUE,
    evaluate_eligibility,
    generate_recommendations,
    held_product_rule,
)
from pbip_ml.recommendations.engine import (
    MODEL_VERSION as RULES_VERSION,
)
from pbip_ml.recommendations.propensity import (
    FEATURE_NAMES,
    MANIFEST,
    MODEL_FILE,
    MODEL_VERSION,
    SCORE_LABEL,
    ProductModel,
    PropensityLoadError,
    PropensityModel,
    feature_vector,
    load,
)
from pbip_ml.recommendations.simulation import Scenario, Snapshot, simulate

pytestmark = pytest.mark.unit

CATALOGUE_IDS = frozenset(product.product_id for product in PRODUCT_CATALOGUE)
N = len(FEATURE_NAMES)


@pytest.fixture(scope="module")
def panel() -> list[Snapshot]:
    return simulate(seed=7, customers=120, months=14)


def _model(favourite: UUID | None = None) -> PropensityModel:
    """A hand-set model: every product near 1%, ``favourite`` near 100% whatever the customer."""
    products = {
        pid: ProductModel(8.0 if pid == favourite else -4.6, (0.0,) * N, (0.0,) * N, (1.0,) * N)
        for pid in CATALOGUE_IDS
    }
    return PropensityModel(MODEL_VERSION, products)


def _ctx(**overrides: object) -> CustomerRecommendationContext:
    values: dict[str, object] = {
        "customer_id": uuid4(),
        "segment": "BUDGET_CONSCIOUS",
        "health_score": 35,
        "monthly_inflow_minor": 30_000_00,
        "monthly_spend_minor": 32_000_00,
        "savings_ratio": -0.05,
        "discretionary_ratio": 0.10,
        "buffer_months": 0.0,
        "emi_ratio": 0.30,
        "total_balance_minor": 0,
    }
    values.update(overrides)
    return CustomerRecommendationContext.model_validate(values)


# --- The simulation -------------------------------------------------------------------------


def test_the_simulation_is_deterministic_for_a_seed_and_changes_with_it() -> None:
    first = simulate(seed=3, customers=25, months=10)
    again = simulate(seed=3, customers=25, months=10)
    other = simulate(seed=4, customers=25, months=10)

    assert first == again
    assert first != other


def test_a_customers_history_does_not_depend_on_the_population_size() -> None:
    small = [s for s in simulate(seed=3, customers=5, months=10) if s.customer == 4]
    large = [s for s in simulate(seed=3, customers=30, months=10) if s.customer == 4]

    assert small == large


def test_features_at_a_month_use_no_later_month() -> None:
    """Simulating more months never changes what an earlier month's context or label was: the
    context at month t is built from months <= t only, the label from month t + 1 only."""
    short = simulate(seed=5, customers=30, months=9)
    long = simulate(seed=5, customers=30, months=15)
    long_prefix = [s for s in long if s.month <= max(s.month for s in short)]

    assert short == long_prefix


def test_the_simulation_only_lets_customers_take_up_what_the_rules_offered(
    panel: list[Snapshot],
) -> None:
    by_id = {product.product_id: product for product in PRODUCT_CATALOGUE}
    for snap in panel:
        assert snap.taken_next_month <= set(snap.offerable)
        for pid in snap.offerable:
            assert evaluate_eligibility(by_id[pid], snap.context)
            assert held_product_rule(by_id[pid], snap.context)[0]


def test_the_simulated_panel_has_uptake_for_every_product_and_no_age_band() -> None:
    snaps = simulate(seed=11, customers=150, months=14)
    taken = {pid for snap in snaps for pid in snap.taken_next_month}

    assert taken == CATALOGUE_IDS
    assert {snap.context.age_band for snap in snaps} == {"unknown"}  # ADR-0015


# --- Features, split and training -----------------------------------------------------------


def test_the_age_band_and_customer_id_are_not_model_inputs() -> None:
    base = _ctx()
    assert feature_vector(base) == feature_vector(
        base.model_copy(update={"age_band": "60+", "customer_id": uuid4()})
    )
    assert not [name for name in FEATURE_NAMES if "age" in name]


def test_the_time_split_keeps_every_training_label_before_the_first_test_month(
    panel: list[Snapshot],
) -> None:
    split = training.time_split(training.build_rows(panel), train_last_feature_month=7)

    # a row's label is observed in the month after its feature month
    assert int(split.train.month.max()) + 1 < int(split.test.month.min())
    assert not set(split.train.group.tolist()) & set(split.test.group.tolist())
    assert len(split.train.label) and len(split.test.label)


def test_training_and_evaluation_report_metrics_for_both_rankers(panel: list[Snapshot]) -> None:
    split = training.time_split(training.build_rows(panel), train_last_feature_month=7)
    model = training.fit(split.train)
    evaluation = training.evaluate(model, split.test)

    for scorer in (MODEL_VERSION, RULES_VERSION):
        ranking = evaluation[scorer]["ranking"]
        for key in ("precision_at_1", "precision_at_3", "recall_at_1", "recall_at_3"):
            assert 0.0 <= ranking[key] <= 1.0
        assert set(evaluation[scorer]["per_product"]) == {p.title for p in PRODUCT_CATALOGUE}
    for entry in evaluation[MODEL_VERSION]["per_product"].values():
        assert {"brier", "ece", "mean_probability"} <= set(entry)
    assert "ece" not in next(iter(evaluation[RULES_VERSION]["per_product"].values()))
    # The fitted model scores rows exactly as the serving code does.
    rows = split.test
    served = [
        model.products[PRODUCT_CATALOGUE[int(p)].product_id].probability(list(x))
        for p, x in zip(rows.product[:50], rows.features[:50], strict=True)
    ]
    assert np.allclose(served, training.predict(model, rows)[:50])


def test_the_ranking_metrics_on_a_hand_worked_example() -> None:
    rows = training.Rows(
        group=np.array([0, 0, 0, 1, 1, 2]),
        month=np.zeros(6, dtype=np.int64),
        product=np.array([0, 1, 2, 0, 1, 0]),
        features=np.zeros((6, N)),
        rules_score=np.zeros(6),
        label=np.array([0, 1, 0, 1, 1, 0]),
    )
    metrics = training._ranking(rows, np.array([0.9, 0.5, 0.1, 0.2, 0.8, 0.3]))

    assert metrics["customer_months_with_uptake"] == 2  # group 2 has no uptake
    assert metrics["precision_at_1"] == pytest.approx(0.5)  # group 0 misses, group 1 hits
    assert metrics["recall_at_3"] == pytest.approx(1.0)
    assert metrics["precision_at_3"] == pytest.approx((1 / 3 + 2 / 3) / 2)


def _evaluation(model_auc: float, rules_auc: float, ece: float) -> dict[str, Any]:
    def side(auc: float, with_ece: bool) -> dict[str, Any]:
        entry = {"pr_auc": auc, **({"ece": ece} if with_ece else {})}
        return {
            "macro_pr_auc": auc,
            "ranking": {"precision_at_1": auc, "recall_at_3": auc},
            "per_product": {p.title: dict(entry) for p in PRODUCT_CATALOGUE},
        }

    return {MODEL_VERSION: side(model_auc, True), RULES_VERSION: side(rules_auc, False)}


def test_the_gate_promotes_only_a_calibrated_model_that_beats_rules_v1() -> None:
    assert training.gate(_evaluation(0.30, 0.20, 0.005))["promoted"]
    assert not training.gate(_evaluation(0.20, 0.20, 0.005))["promoted"]  # no gain
    assert not training.gate(_evaluation(0.19, 0.20, 0.005))["promoted"]  # worse
    assert not training.gate(_evaluation(0.30, 0.20, 0.08))["promoted"]  # miscalibrated


# --- The artefact ---------------------------------------------------------------------------


def _write(out: Path, promoted: bool) -> None:
    report = {"gate": {"promoted": promoted, "checks": {}}}
    training.write_artifacts(_model(INDEX_SIP_ID), report, out)


def test_a_promoted_artefact_round_trips_and_says_it_was_trained_on_simulated_uptake(
    tmp_path: Path,
) -> None:
    _write(tmp_path, promoted=True)
    loaded = load(tmp_path, CATALOGUE_IDS)

    assert loaded == _model(INDEX_SIP_ID)
    assert "SIMULATED" in json.loads((tmp_path / MODEL_FILE).read_text())["trained_on"]
    assert "simulated uptake" in SCORE_LABEL


def test_a_model_that_did_not_beat_rules_v1_is_never_written_or_loaded(tmp_path: Path) -> None:
    _write(tmp_path, promoted=True)
    _write(tmp_path, promoted=False)  # a later run that failed the gate removes the old model

    assert not (tmp_path / MODEL_FILE).exists()
    assert not (tmp_path / MANIFEST).exists()
    with pytest.raises(PropensityLoadError):
        load(tmp_path, CATALOGUE_IDS)


def test_a_manifest_without_the_gate_verdict_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, promoted=True)
    manifest = json.loads((tmp_path / MANIFEST).read_text())
    manifest["promoted"] = False
    (tmp_path / MANIFEST).write_text(json.dumps(manifest))

    with pytest.raises(PropensityLoadError, match="promotion gate"):
        load(tmp_path, CATALOGUE_IDS)


def test_an_edited_model_file_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, promoted=True)
    raw = json.loads((tmp_path / MODEL_FILE).read_text())
    raw["products"][str(INDEX_SIP_ID)]["intercept"] = -50.0
    (tmp_path / MODEL_FILE).write_text(json.dumps(raw))

    with pytest.raises(PropensityLoadError, match="does not match"):
        load(tmp_path, CATALOGUE_IDS)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda raw: raw.update(feature_names=["x"] * N), "feature list"),
        (lambda raw: raw.update(model_version="sim-propensity-v0"), "model version"),
        (lambda raw: raw["products"].popitem(), "catalogue"),
    ],
)
def test_an_artefact_that_does_not_fit_this_code_is_refused(
    tmp_path: Path, edit: Callable[[dict[str, Any]], object], message: str
) -> None:
    _write(tmp_path, promoted=True)
    raw = json.loads((tmp_path / MODEL_FILE).read_text())
    edit(raw)
    (tmp_path / MODEL_FILE).write_text(json.dumps(raw))
    training_manifest = json.loads((tmp_path / MANIFEST).read_text())
    training_manifest[MODEL_FILE] = (
        __import__("hashlib").sha256((tmp_path / MODEL_FILE).read_bytes()).hexdigest()
    )  # a re-signed file: the content checks must still refuse it
    (tmp_path / MANIFEST).write_text(json.dumps(training_manifest))

    with pytest.raises(PropensityLoadError, match=message):
        load(tmp_path, CATALOGUE_IDS)


def test_a_missing_directory_is_a_load_error_not_a_crash(tmp_path: Path) -> None:
    with pytest.raises(PropensityLoadError):
        load(tmp_path / "absent", CATALOGUE_IDS)


# --- Serving: re-rank only what the rules allow ---------------------------------------------


def test_the_model_never_offers_a_product_the_rules_exclude() -> None:
    """A model that wants the SIP for everyone still cannot offer it to a customer the rules
    exclude (no buffer, no balance): it only re-orders the rules' own list."""
    ctx = _ctx()
    assert not evaluate_eligibility(
        next(p for p in PRODUCT_CATALOGUE if p.product_id == INDEX_SIP_ID), ctx
    )

    recs = generate_recommendations(ctx, top_k=6, propensity=_model(INDEX_SIP_ID))
    rules = generate_recommendations(ctx, top_k=6)

    assert INDEX_SIP_ID not in {r.product_id for r in recs}
    assert {r.product_id for r in recs} == {r.product_id for r in rules}


def test_the_model_reorders_the_same_offers_with_the_same_reasons_and_its_own_label(
    panel: list[Snapshot],
) -> None:
    model = _model(FIXED_DEPOSIT_ID)
    for snap in panel[::7]:
        rules = generate_recommendations(snap.context, top_k=6)
        ranked = generate_recommendations(snap.context, top_k=6, propensity=model)

        assert {r.product_id for r in ranked} == {r.product_id for r in rules}
        assert {r.product_id: r.reasons for r in ranked} == {r.product_id: r.reasons for r in rules}
        assert all(r.model_version == MODEL_VERSION for r in ranked)
        if any(r.product_id == FIXED_DEPOSIT_ID for r in rules):
            assert ranked[0].product_id == FIXED_DEPOSIT_ID


def test_a_held_product_stays_excluded_under_the_model() -> None:
    ctx = _ctx(
        health_score=80,
        monthly_inflow_minor=200_000_00,
        monthly_spend_minor=80_000_00,
        savings_ratio=0.6,
        buffer_months=4.0,
        emi_ratio=0.0,
        total_balance_minor=320_000_00,
        held_products=(HeldProduct(kind=HeldProductKind.FIXED_DEPOSIT, amount_minor=1_00),),
    )
    recs = generate_recommendations(ctx, top_k=6, propensity=_model(FIXED_DEPOSIT_ID))

    assert FIXED_DEPOSIT_ID not in {r.product_id for r in recs}  # buffer 4 < 6-month top-up rule


def test_scoring_six_products_is_one_feature_pass_and_one_dot_product_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The request-path cost is fixed by construction, not timed (a CPU-time bound failed on a CI
    runner; #362 precedent): each product score builds the feature vector once and evaluates one
    logistic over exactly len(FEATURE_NAMES) terms, in pure Python with no I/O or NumPy."""
    from pbip_ml.recommendations import propensity as module

    model = _model()
    ctx = _ctx()
    built: list[int] = []
    real_vector = module.feature_vector

    def counting_vector(c: object) -> list[float]:
        vector = real_vector(c)  # type: ignore[arg-type]  # same object the API passes
        built.append(len(vector))
        return vector

    monkeypatch.setattr(module, "feature_vector", counting_vector)
    exps: list[float] = []
    real_exp = math.exp
    monkeypatch.setattr(module.math, "exp", lambda z: exps.append(z) or real_exp(z))
    for pid in CATALOGUE_IDS:
        assert 0.0 < model.score(pid, ctx) < 1.0
    assert built == [len(module.FEATURE_NAMES)] * len(CATALOGUE_IDS)
    assert len(exps) == len(CATALOGUE_IDS)  # one logistic per product, no hidden extra work
    for product in model.products.values():
        assert len(product.coefficients) == len(module.FEATURE_NAMES)


def test_probabilities_are_finite_for_extreme_inputs() -> None:
    model = PropensityModel(
        MODEL_VERSION,
        {pid: ProductModel(0.0, (50.0,) * N, (0.0,) * N, (1e-3,) * N) for pid in CATALOGUE_IDS},
    )
    for ctx in (_ctx(), _ctx(monthly_inflow_minor=10**15, total_balance_minor=10**15)):
        for pid in CATALOGUE_IDS:
            assert math.isfinite(model.score(pid, ctx))


def test_the_rules_follow_scenario_runs() -> None:
    """The sanity scenario (uptake follows rules-v1) produces labelled rows too."""
    snaps = simulate(seed=9, customers=40, months=8, scenario=Scenario.RULES_FOLLOW)
    assert any(snap.taken_next_month for snap in snaps)
