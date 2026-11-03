"""Train and evaluate the simulated-uptake propensity model against rules-v1 (TASK-054, ADR-0049).

Offline only (import-linter keeps it off the request path). Steps:

1. ``build_rows``: one row per (customer-month, product the rules allowed that month), with the
   serving ``feature_vector`` of the context, rules-v1's ``relevance_score`` and the label "taken
   up during the following month".
2. ``time_split``: train on the earlier feature months, test on the later ones, with a one-month
   gap so that no training label falls in or after the first test month.
3. ``fit``: one standardised logistic regression per product (scikit-learn), no class weights,
   so the probabilities stay calibrated to the simulated base rates.
4. ``evaluate``: per product PR-AUC, Brier score, expected calibration error; per customer-month
   precision@k and recall@k of the ranking; the same numbers for rules-v1 on the same rows.
5. ``gate``: the model is promoted only if it beats rules-v1 on the documented scenario.

All results describe the SIMULATION, never real customers.
"""

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss

from pbip_ml.recommendations.engine import (
    MODEL_VERSION as RULES_VERSION,
)
from pbip_ml.recommendations.engine import (
    PRODUCT_CATALOGUE,
    relevance_score,
)
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
    sha256_file,
)
from pbip_ml.recommendations.simulation import Snapshot

_BY_ID = {product.product_id: product for product in PRODUCT_CATALOGUE}
TOP_K = (1, 3)
CALIBRATION_BINS = 10
# Promotion gate on the documented scenario's test months (ADR-0049).
GATE_MIN_MACRO_PR_AUC_GAIN = 0.02  # absolute gain over rules-v1, mean over products
GATE_MAX_PRODUCT_PR_AUC_LOSS = 0.0  # no product may rank worse than rules-v1
GATE_MAX_ECE = 0.02  # each product's probabilities within 2 points of the observed rate


@dataclass(frozen=True, slots=True)
class Rows:
    """Parallel arrays, one entry per (customer-month, offerable product)."""

    group: np.ndarray  # customer-month id, for the ranking metrics
    month: np.ndarray  # feature month (the label is the month after)
    product: np.ndarray  # index into PRODUCT_CATALOGUE
    features: np.ndarray
    rules_score: np.ndarray
    label: np.ndarray

    def take(self, mask: np.ndarray) -> "Rows":
        return Rows(
            self.group[mask],
            self.month[mask],
            self.product[mask],
            self.features[mask],
            self.rules_score[mask],
            self.label[mask],
        )


_INDEX = {product.product_id: i for i, product in enumerate(PRODUCT_CATALOGUE)}


def build_rows(snapshots: list[Snapshot]) -> Rows:
    group, month, product, features, rules, label = [], [], [], [], [], []
    for g, snap in enumerate(snapshots):
        vector = feature_vector(snap.context)
        for pid in snap.offerable:
            group.append(g)
            month.append(snap.month)
            product.append(_INDEX[pid])
            features.append(vector)
            rules.append(relevance_score(_BY_ID[pid], snap.context))
            label.append(1 if pid in snap.taken_next_month else 0)
    return Rows(
        np.array(group, dtype=np.int64),
        np.array(month, dtype=np.int64),
        np.array(product, dtype=np.int64),
        np.array(features, dtype=np.float64).reshape(-1, len(FEATURE_NAMES)),
        np.array(rules, dtype=np.float64),
        np.array(label, dtype=np.int64),
    )


@dataclass(frozen=True, slots=True)
class Split:
    train: Rows
    test: Rows
    train_last_feature_month: int
    test_first_feature_month: int


def time_split(rows: Rows, train_last_feature_month: int) -> Split:
    """Train on feature months <= cut, test on months >= cut + 2. A row's label is the month after
    its feature month, so every training label (<= cut + 1) is before the first test month."""
    first_test = train_last_feature_month + 2
    return Split(
        rows.take(rows.month <= train_last_feature_month),
        rows.take(rows.month >= first_test),
        train_last_feature_month,
        first_test,
    )


def fit(train: Rows) -> PropensityModel:
    products: dict[UUID, ProductModel] = {}
    for index, product in enumerate(PRODUCT_CATALOGUE):
        mask = train.product == index
        x, y = train.features[mask], train.label[mask]
        if y.sum() == 0 or y.sum() == len(y):
            raise ValueError(f"{product.title}: the training rows need both outcomes")
        means = x.mean(axis=0)
        scales = x.std(axis=0)
        scales[scales == 0] = 1.0  # a constant column (never set for this product) has no effect
        model = LogisticRegression(C=1.0, max_iter=2000)
        model.fit((x - means) / scales, y)
        products[product.product_id] = ProductModel(
            float(model.intercept_[0]),
            tuple(float(v) for v in model.coef_[0]),
            tuple(float(v) for v in means),
            tuple(float(v) for v in scales),
        )
    return PropensityModel(MODEL_VERSION, products)


def predict(model: PropensityModel, rows: Rows) -> np.ndarray:
    out = np.empty(len(rows.label), dtype=np.float64)
    for index, product in enumerate(PRODUCT_CATALOGUE):
        mask = rows.product == index
        params = model.products[product.product_id]
        z = params.intercept + ((rows.features[mask] - params.means) / params.scales) @ np.array(
            params.coefficients
        )
        out[mask] = 1.0 / (1.0 + np.exp(-z))
    return out


def expected_calibration_error(probability: np.ndarray, label: np.ndarray) -> float:
    """Equal-count bins (the probabilities are small and skewed, so equal-width bins sit empty)."""
    order = np.argsort(probability, kind="stable")
    error = 0.0
    for chunk in np.array_split(order, CALIBRATION_BINS):
        if len(chunk):
            error += len(chunk) * abs(probability[chunk].mean() - label[chunk].mean())
    return float(error / len(probability))


def _ranking(rows: Rows, score: np.ndarray) -> dict[str, float]:
    """precision@k and recall@k over customer-months with at least one uptake. Ties keep the
    catalogue order, as the engine's stable sort does."""
    hits: dict[int, list[float]] = {k: [] for k in TOP_K}
    recalls: dict[int, list[float]] = {k: [] for k in TOP_K}
    order = np.lexsort((rows.product, -score, rows.group))
    groups, starts = np.unique(rows.group[order], return_index=True)
    bounds = [*starts[1:], len(order)]
    for start, end in zip(starts, bounds, strict=True):
        labels = rows.label[order[start:end]]
        positives = int(labels.sum())
        if positives == 0:
            continue
        for k in TOP_K:
            found = int(labels[:k].sum())
            hits[k].append(found / k)
            recalls[k].append(found / positives)
    metrics = {"customer_months_with_uptake": float(len(hits[TOP_K[0]]))}
    metrics["customer_months"] = float(len(groups))
    for k in TOP_K:
        metrics[f"precision_at_{k}"] = float(np.mean(hits[k])) if hits[k] else 0.0
        metrics[f"recall_at_{k}"] = float(np.mean(recalls[k])) if recalls[k] else 0.0
    return metrics


def _per_product(rows: Rows, score: np.ndarray, is_probability: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for index, product in enumerate(PRODUCT_CATALOGUE):
        mask = rows.product == index
        y, s = rows.label[mask], score[mask]
        entry: dict[str, Any] = {"rows": int(mask.sum()), "uptakes": int(y.sum())}
        entry["observed_rate"] = float(y.mean()) if mask.any() else 0.0
        entry["pr_auc"] = float(average_precision_score(y, s)) if 0 < y.sum() < len(y) else None
        if is_probability and mask.any():
            entry["mean_probability"] = float(s.mean())
            entry["brier"] = float(brier_score_loss(y, s))
            entry["ece"] = expected_calibration_error(s, y)
        out[product.title] = entry
    return out


def _macro(per_product: dict[str, Any]) -> float:
    values = [entry["pr_auc"] for entry in per_product.values() if entry["pr_auc"] is not None]
    return float(np.mean(values)) if values else 0.0


def evaluate(model: PropensityModel, rows: Rows) -> dict[str, Any]:
    probability = predict(model, rows)
    model_products = _per_product(rows, probability, is_probability=True)
    rules_products = _per_product(rows, rows.rules_score, is_probability=False)
    return {
        "rows": len(rows.label),
        "uptakes": int(rows.label.sum()),
        MODEL_VERSION: {
            "ranking": _ranking(rows, probability),
            "per_product": model_products,
            "macro_pr_auc": _macro(model_products),
        },
        RULES_VERSION: {
            "ranking": _ranking(rows, rows.rules_score),
            "per_product": rules_products,
            "macro_pr_auc": _macro(rules_products),
            "note": "rules-v1's score is not a probability, so no calibration is reported for it",
        },
    }


def gate(evaluation: dict[str, Any]) -> dict[str, Any]:
    """Promote only if the model ranks better than rules-v1 overall, on every product, at the top
    of each list, and its probabilities are calibrated (on the simulated test months)."""
    model, rules = evaluation[MODEL_VERSION], evaluation[RULES_VERSION]
    checks: dict[str, bool] = {
        "macro_pr_auc_gain": model["macro_pr_auc"]
        >= rules["macro_pr_auc"] + GATE_MIN_MACRO_PR_AUC_GAIN,
        "precision_at_1_not_worse": model["ranking"]["precision_at_1"]
        >= rules["ranking"]["precision_at_1"],
        "recall_at_3_not_worse": model["ranking"]["recall_at_3"] >= rules["ranking"]["recall_at_3"],
    }
    for title, entry in model["per_product"].items():
        baseline = rules["per_product"][title]["pr_auc"]
        checks[f"pr_auc_not_worse:{title}"] = (
            entry["pr_auc"] is not None
            and baseline is not None
            and entry["pr_auc"] >= baseline - GATE_MAX_PRODUCT_PR_AUC_LOSS
        )
        checks[f"ece:{title}"] = entry.get("ece", math.inf) <= GATE_MAX_ECE
    return {"promoted": all(checks.values()), "checks": checks}


def model_json(model: PropensityModel) -> dict[str, Any]:
    return {
        "format": ARTIFACT_FORMAT,
        "model_version": MODEL_VERSION,
        "trained_on": "SIMULATED product uptake (pbip_ml.recommendations.simulation)",
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


def write_artifacts(model: PropensityModel, report: dict[str, Any], out: Path) -> None:
    """Write the report always. Write the model and a manifest that ``propensity.load`` accepts
    only if the gate passed; otherwise remove any older model so the API keeps rules-v1."""
    out.mkdir(parents=True, exist_ok=True)
    (out / REPORT_FILE).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    promoted = bool(report["gate"]["promoted"])
    if not promoted:
        for name in (MODEL_FILE, MANIFEST):
            (out / name).unlink(missing_ok=True)
        return
    (out / MODEL_FILE).write_text(json.dumps(model_json(model), indent=2) + "\n", encoding="utf-8")
    manifest: dict[str, object] = {name: sha256_file(out / name) for name in ARTIFACTS}
    manifest["promoted"] = promoted
    (out / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
