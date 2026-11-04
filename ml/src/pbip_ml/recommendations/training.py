"""Train two rankers on SIMULATED uptake and gate them against rules-v1 (TASK-054, issue #77).

Offline only (import-linter keeps it off the request path). The rows come from ``panel.py``:
eligible candidates only, labelled with the simulated uptake of the following month.

* ``sim-propensity-v2``: a logistic-regression baseline, one standardised regression per product
  (the ``propensity.PropensityModel`` form, scored in pure Python at serving time).
* ``sim-lgbm-ranker-v2``: one LightGBM ``lambdarank`` model over all products, grouped by
  customer-month, with the product as a categorical input.

Both see exactly ``propensity.feature_vector(ctx)``: the served context, no clicks, no age band.

The gate (fixed before the first run; see ``docs/ml/recommendation-propensity-decision.md``):
the candidate is whichever model has the higher NDCG@3 on the VALIDATION months; it is served only
if, on the held-out TEST months, it beats rules-v1 on NDCG@3 and on hit-rate@3 (point estimate
higher AND the 95% customer-level bootstrap interval of the difference above zero), the engine
with it never shows a product the rules exclude and never changes a reason, and its added
inference p95 is at most ``GATE_MAX_ADDED_P95_MS``. Only a model with a served form can be
promoted: today that is the logistic regression (``propensity.py``); serving the ranker would
need its own reviewed artefact format and loader. All numbers describe the SIMULATION.
"""

import hashlib
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import lightgbm as lgb
import numpy as np
from sklearn.linear_model import LogisticRegression

from pbip_ml.recommendations.contract import CustomerRecommendationContext
from pbip_ml.recommendations.engine import (
    MODEL_VERSION as RULES_VERSION,
)
from pbip_ml.recommendations.engine import (
    PRODUCT_CATALOGUE,
    evaluate_eligibility,
    generate_recommendations,
    held_product_rule,
    relevance_score,
)
from pbip_ml.recommendations.panel import Rows, candidates
from pbip_ml.recommendations.propensity import (
    ARTIFACT_FORMAT,
    ARTIFACTS,
    FEATURE_NAMES,
    MANIFEST,
    MODEL_FILE,
    MODEL_VERSION,
    REPORT_FILE,
    ProductModel,
    PropensityModel,
    feature_vector,
)

LR_VERSION: Final = MODEL_VERSION  # the served name of the logistic regression
POPULARITY: Final = "popularity (diagnostic: training uptake rate per product)"
SERVABLE: Final = frozenset({LR_VERSION})
LGBM_VERSION: Final = "sim-lgbm-ranker-v2"
K: Final = 3
GATE_MAX_ADDED_P95_MS: Final = 2.0  # 5% of the 40 ms endpoint budget (#343)
BOOTSTRAP_RESAMPLES: Final = 1000
BOOTSTRAP_SEED: Final = 0
LATENCY_CONTEXTS: Final = 2000
ENGINE_CHECK_CONTEXTS: Final = 2000
_BY_ID: Final = {product.product_id: product for product in PRODUCT_CATALOGUE}

# Fixed before the first run; early stopping on the validation months picks the round count.
RANKER_PARAMS: Final[dict[str, Any]] = {
    "objective": "lambdarank",
    "metric": "ndcg",
    "eval_at": [K],
    "learning_rate": 0.05,
    "num_leaves": 15,
    "min_data_in_leaf": 100,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "seed": 42,
    "deterministic": True,
    "num_threads": 1,
    "verbose": -1,
}
RANKER_MAX_ROUNDS: Final = 1000
RANKER_EARLY_STOPPING: Final = 50


# --- The two models -----------------------------------------------------------------------------


def fit_logistic(train: Rows) -> PropensityModel:
    """One standardised logistic regression per product, no class weights."""
    products: dict[UUID, ProductModel] = {}
    for index, product in enumerate(PRODUCT_CATALOGUE):
        mask = train.product == index
        x, y = train.features[mask], train.label[mask]
        if y.sum() == 0 or y.sum() == len(y):
            raise ValueError(f"{product.title}: the training rows need both outcomes")
        means = x.mean(axis=0)
        scales = x.std(axis=0)
        scales[scales == 0] = 1.0  # a constant column has no effect
        model = LogisticRegression(C=1.0, max_iter=2000)
        model.fit((x - means) / scales, y)
        products[product.product_id] = ProductModel(
            float(model.intercept_[0]),
            tuple(float(v) for v in model.coef_[0]),
            tuple(float(v) for v in means),
            tuple(float(v) for v in scales),
        )
    return PropensityModel(MODEL_VERSION, products)


def predict_logistic(model: PropensityModel, rows: Rows) -> np.ndarray:
    out = np.empty(len(rows.label), dtype=np.float64)
    for index, product in enumerate(PRODUCT_CATALOGUE):
        mask = rows.product == index
        params = model.products[product.product_id]
        z = params.intercept + ((rows.features[mask] - params.means) / params.scales) @ np.array(
            params.coefficients
        )
        out[mask] = 1.0 / (1.0 + np.exp(-z))
    return out


def popularity(train: Rows, rows: Rows) -> np.ndarray:
    """Each product's training uptake rate, the same for every customer: a reference that shows
    how much of a ranking gain comes only from knowing which products are taken up more often."""
    rates = np.array(
        [train.label[train.product == i].mean() for i in range(len(PRODUCT_CATALOGUE))]
    )
    return np.asarray(rates[rows.product], dtype=np.float64)


def ranker_matrix(rows: Rows) -> np.ndarray:
    """The context features plus the product index (a categorical input)."""
    return np.hstack([rows.features, rows.product[:, None].astype(np.float64)])


def _group_sizes(rows: Rows) -> np.ndarray:
    if len(rows.group) and not bool(np.all(np.diff(rows.group) >= 0)):
        raise ValueError("rows must be grouped contiguously by customer-month")
    return np.unique(rows.group, return_counts=True)[1]


def _dataset(rows: Rows) -> lgb.Dataset:
    return lgb.Dataset(
        ranker_matrix(rows),
        label=rows.label,
        group=_group_sizes(rows),
        categorical_feature=[rows.features.shape[1]],
        free_raw_data=False,
    )


def fit_ranker(train: Rows, valid: Rows) -> lgb.Booster:
    return lgb.train(
        RANKER_PARAMS,
        _dataset(train),
        num_boost_round=RANKER_MAX_ROUNDS,
        valid_sets=[_dataset(valid)],
        callbacks=[lgb.early_stopping(RANKER_EARLY_STOPPING, verbose=False)],
    )


def predict_ranker(booster: lgb.Booster, rows: Rows) -> np.ndarray:
    return np.asarray(booster.predict(ranker_matrix(rows)), dtype=np.float64)


_PRODUCT_INDEX: Final = {p.product_id: float(i) for i, p in enumerate(PRODUCT_CATALOGUE)}


@dataclass(frozen=True, slots=True)
class RankerOrdering:
    """The LightGBM ranker through the engine's ``OrderingModel`` interface, for the offline
    eligibility and reasons checks. The score is the logistic of the ranker's raw score, which
    keeps the order and fits the contract's [0, 1] range."""

    booster: lgb.Booster
    version: str = LGBM_VERSION

    def score(self, product_id: UUID, ctx: CustomerRecommendationContext) -> float:
        row = np.array([[*feature_vector(ctx), _PRODUCT_INDEX[product_id]]])
        raw = float(self.booster.predict(row)[0])
        return 1.0 / (1.0 + math.exp(-raw))


# --- Ranking metrics ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GroupMetrics:
    """One entry per customer-month with at least one uptake among its candidates."""

    customer: np.ndarray
    candidates: np.ndarray
    ndcg: np.ndarray
    hit: np.ndarray
    precision_at_1: np.ndarray
    reciprocal_rank: np.ndarray


_DISCOUNT: Final = 1.0 / np.log2(np.arange(2, K + 2))


def per_group(rows: Rows, score: np.ndarray) -> GroupMetrics:
    """NDCG@3 (binary relevance), hit-rate@3, precision@1 and reciprocal rank per customer-month.
    Ties keep the catalogue order, as the engine's stable sort does."""
    order = np.lexsort((rows.product, -score, rows.group))
    _, starts = np.unique(rows.group[order], return_index=True)
    ends = [*starts[1:], len(order)]
    out: dict[str, list[float]] = {k: [] for k in ("customer", "n", "ndcg", "hit", "p1", "rr")}
    for start, end in zip(starts, ends, strict=True):
        labels = rows.label[order[start:end]]
        positives = int(labels.sum())
        if positives == 0:
            continue
        top = labels[:K]
        ideal = float(_DISCOUNT[: min(positives, K)].sum())
        out["customer"].append(float(rows.customer[order[start]]))
        out["n"].append(float(end - start))
        out["ndcg"].append(float((top * _DISCOUNT[: len(top)]).sum()) / ideal)
        out["hit"].append(float(top.max()))
        out["p1"].append(float(labels[0]))
        out["rr"].append(1.0 / (1 + int(np.argmax(labels))))
    return GroupMetrics(*(np.array(out[k]) for k in ("customer", "n", "ndcg", "hit", "p1", "rr")))


def summarise(metrics: GroupMetrics) -> dict[str, float]:
    def mean(values: np.ndarray) -> float:
        return float(values.mean()) if len(values) else 0.0

    return {
        "customer_months_with_uptake": float(len(metrics.ndcg)),
        "ndcg_at_3": mean(metrics.ndcg),
        "hit_rate_at_3": mean(metrics.hit),
        "precision_at_1": mean(metrics.precision_at_1),
        "mrr": mean(metrics.reciprocal_rank),
        "share_with_more_than_3_candidates": mean((metrics.candidates > K).astype(np.float64)),
    }


def bootstrap_difference(
    customer: np.ndarray, model: np.ndarray, rules: np.ndarray
) -> tuple[float, float]:
    """95% interval of mean(model) - mean(rules), resampling CUSTOMERS (a customer's months are
    correlated, so resampling customer-months would understate the uncertainty)."""
    ids, inverse = np.unique(customer, return_inverse=True)
    diff_sum = np.bincount(inverse, weights=model - rules, minlength=len(ids))
    count = np.bincount(inverse, minlength=len(ids)).astype(np.float64)
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    draws = rng.integers(0, len(ids), size=(BOOTSTRAP_RESAMPLES, len(ids)))
    estimates = diff_sum[draws].sum(axis=1) / count[draws].sum(axis=1)
    low, high = np.percentile(estimates, [2.5, 97.5])
    return float(low), float(high)


def _difference(base: GroupMetrics, other: GroupMetrics) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for metric, attr in (("ndcg_at_3", "ndcg"), ("hit_rate_at_3", "hit")):
        mine, theirs = getattr(other, attr), getattr(base, attr)
        out[metric] = {
            "difference": float(mine.mean() - theirs.mean()),
            "ci95": list(bootstrap_difference(base.customer, mine, theirs)),
        }
    return out


def compare(rows: Rows, scores: dict[str, np.ndarray]) -> dict[str, Any]:
    """Every ranker's metrics on ``rows``, and each model's difference from rules-v1 with its
    bootstrap interval. ``scores`` must include rules-v1."""
    groups = {name: per_group(rows, score) for name, score in scores.items()}
    base = groups[RULES_VERSION]
    return {
        "metrics": {name: summarise(g) for name, g in groups.items()},
        "difference_vs_rules_v1": {
            name: _difference(base, g) for name, g in groups.items() if name != RULES_VERSION
        },
    }


# --- Serving-shape checks: eligibility first, reasons, latency -----------------------------------


def engine_checks(
    ordering: PropensityModel | RankerOrdering,
    contexts: tuple[CustomerRecommendationContext, ...],
) -> dict[str, int]:
    """Run the real engine with the candidate ordering. Counts any shown product the rules
    exclude and any reason that differs from rules-v1's for the same product."""
    violations = mismatches = 0
    for ctx in contexts:
        ranked = generate_recommendations(ctx, top_k=K, propensity=ordering)
        rules = {r.product_id: r.reasons for r in generate_recommendations(ctx, top_k=6)}
        for rec in ranked:
            product = _BY_ID[rec.product_id]
            if not (evaluate_eligibility(product, ctx) and held_product_rule(product, ctx)[0]):
                violations += 1
            if rules.get(rec.product_id) != rec.reasons:
                mismatches += 1
    return {
        "contexts_checked": len(contexts),
        "ineligible_products_shown": violations,
        "reasons_differing_from_rules_v1": mismatches,
    }


def _percentiles(samples_ns: list[int]) -> dict[str, float]:
    p50, p95 = np.percentile(np.array(samples_ns, dtype=np.float64) / 1e6, [50, 95])
    return {"p50_ms": round(float(p50), 4), "p95_ms": round(float(p95), 4)}


def measure_latency(
    lr: PropensityModel, booster: lgb.Booster, contexts: tuple[CustomerRecommendationContext, ...]
) -> dict[str, Any]:
    """Wall time to score one request's candidates, as each would be served: rules-v1's formula,
    the LR in pure Python, the ranker as one ``Booster.predict`` over the candidate rows. The
    candidate list is built outside the timed part (the engine builds it for every ordering)."""
    samples: dict[str, list[int]] = {RULES_VERSION: [], LR_VERSION: [], LGBM_VERSION: []}
    for ctx in contexts:
        allowed = candidates(ctx)
        start = time.perf_counter_ns()
        _ = [relevance_score(_BY_ID[pid], ctx) for pid in allowed]
        samples[RULES_VERSION].append(time.perf_counter_ns() - start)
        start = time.perf_counter_ns()
        _ = [lr.score(pid, ctx) for pid in allowed]
        samples[LR_VERSION].append(time.perf_counter_ns() - start)
        start = time.perf_counter_ns()
        vector = feature_vector(ctx)
        _ = booster.predict(np.array([[*vector, _PRODUCT_INDEX[pid]] for pid in allowed]))
        samples[LGBM_VERSION].append(time.perf_counter_ns() - start)
    return {
        "contexts": len(contexts),
        "load_average_1m": round(os.getloadavg()[0], 2),
        **{name: _percentiles(values) for name, values in samples.items()},
        "note": "wall clock per request, scoring only; the rest of the request is unchanged",
    }


def added_p95_ms(latency: dict[str, Any], candidate: str) -> float:
    """What the candidate adds over rules-v1's own scoring, at p95."""
    return float(max(latency[candidate]["p95_ms"] - latency[RULES_VERSION]["p95_ms"], 0.0))


# --- The gate -----------------------------------------------------------------------------------


def choose_candidate(validation: dict[str, Any]) -> str:
    """The model with the higher NDCG@3 on the validation months (never the test months)."""
    metrics = validation["metrics"]
    return max((LGBM_VERSION, LR_VERSION), key=lambda name: metrics[name]["ndcg_at_3"])


def gate(
    candidate: str, test: dict[str, Any], checks: dict[str, int], latency: dict[str, Any]
) -> dict[str, Any]:
    """All must hold for ``candidate`` on the TEST months; otherwise rules-v1 stays."""
    mine, rules = test["metrics"][candidate], test["metrics"][RULES_VERSION]
    diff = test["difference_vs_rules_v1"][candidate]
    results: dict[str, bool] = {}
    for metric in ("ndcg_at_3", "hit_rate_at_3"):
        results[f"{metric}_beats_rules_v1"] = bool(
            mine[metric] > rules[metric] and diff[metric]["ci95"][0] > 0.0
        )
    results["eligibility_first"] = checks["ineligible_products_shown"] == 0
    results["reasons_unchanged"] = checks["reasons_differing_from_rules_v1"] == 0
    added = added_p95_ms(latency, candidate)
    results["added_p95_within_budget"] = added <= GATE_MAX_ADDED_P95_MS
    results["has_a_served_form"] = candidate in SERVABLE
    return {
        "candidate": candidate,
        "promoted": all(results.values()),
        "checks": results,
        "added_p95_ms": round(added, 4),
        "max_added_p95_ms": GATE_MAX_ADDED_P95_MS,
    }


# --- The served artefact (only for a promoted logistic regression) -------------------------------


def model_json(model: PropensityModel) -> dict[str, Any]:
    return {
        "format": ARTIFACT_FORMAT,
        "model_version": MODEL_VERSION,
        "trained_on": "SIMULATED product uptake (pbip_datagen.uptake, uptake-sim-2)",
        "feature_names": list(FEATURE_NAMES),
        "products": {
            str(pid): {
                "title": _BY_ID[pid].title,
                "intercept": params.intercept,
                "coefficients": list(params.coefficients),
                "means": list(params.means),
                "scales": list(params.scales),
            }
            for pid, params in model.products.items()
        },
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_artifacts(model: PropensityModel, report: dict[str, Any], out: Path) -> None:
    """Write the report always. Write the model and a manifest that ``propensity.load`` accepts
    only if the gate promoted the logistic regression; otherwise remove any older model so the
    API keeps rules-v1."""
    out.mkdir(parents=True, exist_ok=True)
    (out / REPORT_FILE).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    gate_result = report["gate"]
    if not (gate_result["promoted"] and gate_result["candidate"] == LR_VERSION):
        for name in (MODEL_FILE, MANIFEST):
            (out / name).unlink(missing_ok=True)
        return
    (out / MODEL_FILE).write_text(json.dumps(model_json(model), indent=2) + "\n", encoding="utf-8")
    manifest: dict[str, object] = {name: _sha256(out / name) for name in ARTIFACTS}
    manifest["promoted"] = True
    (out / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
