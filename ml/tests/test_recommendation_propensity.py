"""The simulated-uptake ranker experiment (TASK-054 #77, ADR-0049, ADR-0053): the panel leaks no
future data and keeps the eligibility rules first, both models are trained and measured against
rules-v1 with NDCG@3 and hit-rate@3, the gate serves a model only if it beats rules-v1 with the
rules and reasons intact, the artefact loads only when intact and promoted, and everything it
produces says it was trained on simulated uptake."""

import csv
import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import numpy as np
import pytest
from pbip_datagen.__main__ import export_uptake
from pbip_datagen.uptake import PRODUCTS
from pbip_ml.recommendations import train, training
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
from pbip_ml.recommendations.panel import (
    PANEL_FILE,
    PRODUCT_KEYS,
    Rows,
    build_rows,
    candidates,
    context_at,
    read_panel,
    time_split,
)
from pbip_ml.recommendations.propensity import (
    FEATURE_NAMES,
    MANIFEST,
    MODEL_FILE,
    MODEL_VERSION,
    REPORT_FILE,
    SCORE_LABEL,
    ProductModel,
    PropensityLoadError,
    PropensityModel,
    feature_vector,
    load,
)

pytestmark = pytest.mark.unit

CATALOGUE_IDS = frozenset(product.product_id for product in PRODUCT_CATALOGUE)
N = len(FEATURE_NAMES)


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("uptake")
    export_uptake(seed=7, customers=400, months=16, out=out)
    return out


@pytest.fixture(scope="module")
def rows(data_dir: Path) -> Rows:
    return build_rows(read_panel(data_dir))


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


# --- The panel: no leakage, eligibility first, the model sees only the served context ---------


def test_the_data_gen_products_map_exactly_onto_the_catalogue() -> None:
    assert tuple(PRODUCT_KEYS) == PRODUCTS
    assert frozenset(PRODUCT_KEYS.values()) == CATALOGUE_IDS


def test_the_context_at_a_month_uses_no_later_month(data_dir: Path) -> None:
    for history in list(read_panel(data_dir).values())[:40]:
        for t in range(2, len(history)):
            assert context_at(history[: t + 1], t) == context_at(history, t)


def test_only_products_the_rules_allow_become_rows(rows: Rows) -> None:
    """Eligibility first: every candidate passes the eligibility and held-product rules."""
    for g in sorted(set(rows.group.tolist()))[::25]:
        ctx = rows.contexts[g]
        offered = rows.product[rows.group == g]
        assert [PRODUCT_CATALOGUE[int(i)].product_id for i in offered] == list(candidates(ctx))
        for i in offered:
            product = PRODUCT_CATALOGUE[int(i)]
            assert evaluate_eligibility(product, ctx)
            assert held_product_rule(product, ctx)[0]


def test_clicks_persona_and_age_band_are_not_model_inputs(data_dir: Path, tmp_path: Path) -> None:
    """They drive the simulated labels, but the served context has none of them (ADR-0015 for
    the age band), so changing them must not change a single feature or label."""
    with (data_dir / PANEL_FILE).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        raw = list(reader)
    for row in raw:
        row.update({f"clicks_{p}": "9" for p in PRODUCTS}, persona="x", age_band="60+")
    with (tmp_path / PANEL_FILE).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fields)
        writer.writeheader()
        writer.writerows(raw)
    original = build_rows(read_panel(data_dir))
    edited = build_rows(read_panel(tmp_path))
    assert np.array_equal(original.features, edited.features)
    assert np.array_equal(original.label, edited.label)
    assert {ctx.age_band for ctx in edited.contexts} == {"unknown"}


def test_the_age_band_and_customer_id_are_not_features() -> None:
    base = _ctx()
    assert feature_vector(base) == feature_vector(
        base.model_copy(update={"age_band": "60+", "customer_id": uuid4()})
    )
    assert not [name for name in FEATURE_NAMES if "age" in name]


def test_the_time_split_keeps_every_label_before_the_next_block(rows: Rows) -> None:
    split = time_split(rows, train_last=6)
    # a row's label is observed in the month after its feature month
    assert int(split.train.month.max()) + 1 < int(split.valid.month.min())
    assert int(split.valid.month.max()) + 1 < int(split.test.month.min())
    assert len(split.train.label) and len(split.valid.label) and len(split.test.label)


# --- Models and metrics -------------------------------------------------------------------------


def test_both_models_train_and_the_lr_scores_as_the_serving_code_does(rows: Rows) -> None:
    split = time_split(rows, train_last=6)
    lr = training.fit_logistic(split.train)
    ranker = training.fit_ranker(split.train, split.valid)
    scores = {
        RULES_VERSION: split.test.rules_score,
        training.LR_VERSION: training.predict_logistic(lr, split.test),
        training.LGBM_VERSION: training.predict_ranker(ranker, split.test),
    }
    result = training.compare(split.test, scores)

    for name in scores:
        for key in ("ndcg_at_3", "hit_rate_at_3", "precision_at_1", "mrr"):
            assert 0.0 <= result["metrics"][name][key] <= 1.0
    assert set(result["difference_vs_rules_v1"]) == {training.LR_VERSION, training.LGBM_VERSION}
    served = [
        lr.products[PRODUCT_CATALOGUE[int(p)].product_id].probability(list(x))
        for p, x in zip(split.test.product[:50], split.test.features[:50], strict=True)
    ]
    assert np.allclose(served, scores[training.LR_VERSION][:50])


def _rows(group: list[int], product: list[int], label: list[int]) -> Rows:
    n = len(group)
    return Rows(
        group=np.array(group),
        customer=np.array(group),
        month=np.zeros(n, dtype=np.int64),
        product=np.array(product),
        features=np.zeros((n, N)),
        rules_score=np.zeros(n),
        label=np.array(label),
        contexts=(),
    )


def test_ndcg_and_hit_rate_on_a_hand_worked_example() -> None:
    rows = _rows([0, 0, 0, 0, 1, 1, 2], [0, 1, 2, 3, 0, 1, 0], [0, 0, 0, 1, 1, 1, 0])
    metrics = training.per_group(rows, np.array([0.9, 0.8, 0.7, 0.1, 0.2, 0.8, 0.3]))

    assert len(metrics.ndcg) == 2  # group 2 has no uptake
    assert metrics.hit.tolist() == [0.0, 1.0]  # group 0's uptake is ranked 4th
    assert metrics.ndcg.tolist() == pytest.approx([0.0, 1.0])
    assert metrics.reciprocal_rank.tolist() == pytest.approx([0.25, 1.0])
    second = training.per_group(rows, np.array([0.1, 0.9, 0.8, 0.85, 0.2, 0.8, 0.3]))
    assert second.ndcg[0] == pytest.approx(1 / math.log2(3))  # the uptake at rank 2


def test_ties_keep_the_catalogue_order_as_the_engine_does() -> None:
    rows = _rows([0, 0], [0, 1], [0, 1])
    assert training.per_group(rows, np.array([0.5, 0.5])).precision_at_1.tolist() == [0.0]


def test_the_bootstrap_interval_is_zero_for_identical_rankers_and_positive_for_a_better_one() -> (
    None
):
    customers = np.repeat(np.arange(50), 2).astype(np.float64)
    same = np.linspace(0, 1, 100)
    assert training.bootstrap_difference(customers, same, same) == (0.0, 0.0)
    low, _ = training.bootstrap_difference(customers, np.ones(100), np.zeros(100) + 0.5)
    assert low > 0


# --- The gate -----------------------------------------------------------------------------------


def _test_metrics(
    model: float, rules: float, low: float, name: str = MODEL_VERSION
) -> dict[str, Any]:
    return {
        "metrics": {
            name: {"ndcg_at_3": model, "hit_rate_at_3": model},
            RULES_VERSION: {"ndcg_at_3": rules, "hit_rate_at_3": rules},
        },
        "difference_vs_rules_v1": {
            name: {
                "ndcg_at_3": {"difference": model - rules, "ci95": [low, low + 0.1]},
                "hit_rate_at_3": {"difference": model - rules, "ci95": [low, low + 0.1]},
            }
        },
    }


_CLEAN = {"ineligible_products_shown": 0, "reasons_differing_from_rules_v1": 0}


def _latency(added: float, name: str = MODEL_VERSION) -> dict[str, Any]:
    return {RULES_VERSION: {"p95_ms": 0.05}, name: {"p95_ms": 0.05 + added}}


def test_the_gate_promotes_only_a_model_that_beats_rules_v1_with_the_rules_intact() -> None:
    assert training.gate(MODEL_VERSION, _test_metrics(0.7, 0.5, 0.1), _CLEAN, _latency(0.1))[
        "promoted"
    ]
    failing = {
        "no better": (_test_metrics(0.5, 0.5, -0.02), _CLEAN, _latency(0.1)),
        "worse": (_test_metrics(0.4, 0.5, -0.15), _CLEAN, _latency(0.1)),
        "better but within noise": (_test_metrics(0.52, 0.5, -0.01), _CLEAN, _latency(0.1)),
        "shows an ineligible product": (
            _test_metrics(0.7, 0.5, 0.1),
            {**_CLEAN, "ineligible_products_shown": 1},
            _latency(0.1),
        ),
        "changes a reason": (
            _test_metrics(0.7, 0.5, 0.1),
            {**_CLEAN, "reasons_differing_from_rules_v1": 1},
            _latency(0.1),
        ),
        "too slow": (_test_metrics(0.7, 0.5, 0.1), _CLEAN, _latency(5.0)),
    }
    for case, (test, checks, latency) in failing.items():
        assert not training.gate(MODEL_VERSION, test, checks, latency)["promoted"], case


def test_the_gate_never_promotes_a_model_without_a_served_form() -> None:
    name = training.LGBM_VERSION
    verdict = training.gate(name, _test_metrics(0.9, 0.5, 0.2, name), _CLEAN, _latency(0.1, name))
    assert not verdict["promoted"]
    assert verdict["checks"]["has_a_served_form"] is False


def test_the_candidate_is_chosen_on_the_validation_months() -> None:
    def validation(lr: float, lgbm: float) -> dict[str, Any]:
        return {
            "metrics": {
                training.LR_VERSION: {"ndcg_at_3": lr},
                training.LGBM_VERSION: {"ndcg_at_3": lgbm},
            }
        }

    assert training.choose_candidate(validation(0.7, 0.6)) == training.LR_VERSION
    assert training.choose_candidate(validation(0.6, 0.7)) == training.LGBM_VERSION


def test_the_engine_checks_find_no_ineligible_product_and_no_changed_reason(rows: Rows) -> None:
    split = time_split(rows, train_last=6)
    lr = training.fit_logistic(split.train)
    ranker = training.fit_ranker(split.train, split.valid)
    contexts = tuple(rows.contexts[g] for g in sorted(set(split.test.group.tolist()))[:150])
    for ordering in (lr, training.RankerOrdering(ranker)):
        checks = training.engine_checks(ordering, contexts)
        assert checks["ineligible_products_shown"] == 0
        assert checks["reasons_differing_from_rules_v1"] == 0


def test_the_training_command_writes_the_model_only_with_a_promoting_verdict(
    data_dir: Path, tmp_path: Path
) -> None:
    (tmp_path / MODEL_FILE).write_text("stale", encoding="utf-8")
    report = train.run(data_dir, tmp_path)

    assert report["data"].startswith("SIMULATED")
    assert set(report["test"]["metrics"]) == {
        RULES_VERSION,
        training.LR_VERSION,
        training.LGBM_VERSION,
        training.POPULARITY,
    }
    promoted = report["gate"]["promoted"]
    assert (tmp_path / MODEL_FILE).exists() is promoted
    assert (tmp_path / MANIFEST).exists() is promoted
    assert json.loads((tmp_path / REPORT_FILE).read_text())["gate"] == report["gate"]
    if promoted:
        assert load(tmp_path, CATALOGUE_IDS).version == MODEL_VERSION
    else:
        with pytest.raises(PropensityLoadError):
            load(tmp_path, CATALOGUE_IDS)


# --- The artefact ---------------------------------------------------------------------------


def _write(out: Path, promoted: bool) -> None:
    report = {"gate": {"promoted": promoted, "candidate": MODEL_VERSION, "checks": {}}}
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


def test_a_missing_model_file_is_a_load_error_so_rules_v1_serves(tmp_path: Path) -> None:
    _write(tmp_path, promoted=True)
    (tmp_path / MODEL_FILE).unlink()

    with pytest.raises(PropensityLoadError):
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
    rows: Rows,
) -> None:
    model = _model(FIXED_DEPOSIT_ID)
    for ctx in rows.contexts[::40]:
        rules = generate_recommendations(ctx, top_k=6)
        ranked = generate_recommendations(ctx, top_k=6, propensity=model)

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
