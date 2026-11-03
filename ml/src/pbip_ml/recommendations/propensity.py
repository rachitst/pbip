"""Serving side of the simulated-uptake propensity model (TASK-054, ADR-0049). Inference-safe.

The model is one logistic regression per catalogue product, trained offline by
``pbip_ml.recommendations.train`` on a **simulation** of product uptake
(``pbip_ml.recommendations.simulation``), never on real customer behaviour. Its probabilities are
"probability of uptake next month under the simulation's documented assumptions", nothing more,
and every response that uses them says so (``SCORE_LABEL``).

This module has no training code and no third-party imports: the artefact is plain JSON
(coefficients, the standardisation and the feature list), checked against a SHA-256 manifest, and
scoring is a dot product. Nothing is unpickled. It only re-ranks products the eligibility and
held-product rules already allow (``engine.generate_recommendations``); it never adds one.

ADR-0015: ``age_band`` is not a feature. ``customer_id`` is not a feature.
"""

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import UUID

from pbip_ml.recommendations.contract import CustomerRecommendationContext, HeldProductKind

MODEL_VERSION: Final = "sim-propensity-v1"
SCORE_LABEL: Final = "sim-propensity-v1 (trained on simulated uptake, not real customer behaviour)"
ARTIFACT_FORMAT: Final = "pbip-propensity-logreg-json-1"
MODEL_FILE: Final = "propensity_model.json"
REPORT_FILE: Final = "report.json"
MANIFEST: Final = "manifest.json"
ARTIFACTS: Final = (MODEL_FILE, REPORT_FILE)

SEGMENTS: Final = (
    "HIGH_NET_WORTH",
    "BUDGET_CONSCIOUS",
    "FREQUENT_TRANSACTOR",
    "DISCRETIONARY_SPENDER",
    "INACTIVE_SAVER",
)
_HELD_KINDS: Final = tuple(HeldProductKind)

FEATURE_NAMES: Final[tuple[str, ...]] = (
    "log_inflow_k",
    "log_spend_k",
    "savings_ratio",
    "negative_savings",
    "discretionary_ratio",
    "log_discretionary_spend_k",
    "buffer_months",
    "buffer_months_sq",
    "emi_ratio",
    "health",
    "log_balance_k",
    "log_deposit_principal_k",
    *(f"segment_{name}" for name in SEGMENTS),
    *(f"holds_{kind.value}" for kind in _HELD_KINDS),
)


class PropensityLoadError(Exception):
    """The artefact directory is missing, altered, or not a promoted model. Serve rules-v1."""


def _log_k(minor: float) -> float:
    """log(1 + thousands of rupees): incomes and balances are heavy-tailed."""
    return math.log1p(max(minor, 0.0) / 100_000)


def feature_vector(ctx: CustomerRecommendationContext) -> list[float]:
    """The model's inputs, from the same context the rules see. Training uses this function too,
    so training and serving cannot compute a feature differently."""
    savings = min(max(ctx.savings_ratio, -1.0), 1.0)
    buffer = min(ctx.buffer_months, 24.0)
    held = {item.kind for item in ctx.held_products}
    deposits = sum(
        item.amount_minor
        for item in ctx.held_products
        if item.kind == HeldProductKind.FIXED_DEPOSIT
    )
    return [
        _log_k(ctx.monthly_inflow_minor),
        _log_k(ctx.monthly_spend_minor),
        savings,
        max(-savings, 0.0),
        ctx.discretionary_ratio,
        _log_k(ctx.monthly_spend_minor * ctx.discretionary_ratio),
        buffer,
        buffer * buffer / 24.0,
        min(ctx.emi_ratio, 1.5),
        ctx.health_score / 100.0,
        _log_k(ctx.total_balance_minor),
        _log_k(deposits),
        *(1.0 if ctx.segment == name else 0.0 for name in SEGMENTS),
        *(1.0 if kind in held else 0.0 for kind in _HELD_KINDS),
    ]


@dataclass(frozen=True, slots=True)
class ProductModel:
    """A standardised logistic regression: p = sigmoid(b + sum(w * (x - mean) / scale))."""

    intercept: float
    coefficients: tuple[float, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]

    def probability(self, features: list[float]) -> float:
        z = self.intercept
        for x, w, m, s in zip(features, self.coefficients, self.means, self.scales, strict=True):
            z += w * (x - m) / s
        if z >= 0:
            return 1.0 / (1.0 + math.exp(-z))
        e = math.exp(z)  # the numerically stable branch for large negative z
        return e / (1.0 + e)


@dataclass(frozen=True, slots=True)
class PropensityModel:
    version: str
    products: dict[UUID, ProductModel]

    def score(self, product_id: UUID, ctx: CustomerRecommendationContext) -> float:
        """Simulated probability of uptake next month. ``load`` refuses a model that does not
        cover exactly the catalogue, so every catalogue product has coefficients."""
        return self.products[product_id].probability(feature_vector(ctx))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _params(raw: dict[str, object], key: str, positive: bool = False) -> tuple[float, ...]:
    values = raw[key]
    if not isinstance(values, list) or len(values) != len(FEATURE_NAMES):
        raise PropensityLoadError(f"{key} does not match the feature list")
    if not all(isinstance(v, int | float) for v in values):
        raise PropensityLoadError(f"{key} must be numbers")
    out = tuple(float(v) for v in values)
    if not all(math.isfinite(v) and (v > 0 or not positive) for v in out):
        raise PropensityLoadError(f"{key} has a non-finite or non-positive value")
    return out


def _product_model(raw: dict[str, object]) -> ProductModel:
    intercept = raw["intercept"]
    if not isinstance(intercept, int | float) or not math.isfinite(intercept):
        raise PropensityLoadError("bad intercept")
    return ProductModel(
        float(intercept),
        _params(raw, "coefficients"),
        _params(raw, "means"),
        _params(raw, "scales", positive=True),
    )


def _verify_manifest(directory: Path) -> dict[str, object]:
    manifest = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise PropensityLoadError("manifest is not an object")
    for name in ARTIFACTS:
        if manifest.get(name) != sha256_file(directory / name):
            raise PropensityLoadError(f"{name} does not match {MANIFEST}")
    if manifest.get("promoted") is not True:  # the training gate's verdict, written by train
        raise PropensityLoadError("the model did not pass the promotion gate against rules-v1")
    return manifest


def load(directory: Path, catalogue_ids: frozenset[UUID]) -> PropensityModel:
    """Verify the manifest and the gate verdict, then parse the JSON. Any problem raises
    ``PropensityLoadError``; the API then serves rules-v1 (fail safe)."""
    try:
        _verify_manifest(directory)
        raw = json.loads((directory / MODEL_FILE).read_text(encoding="utf-8"))
        if raw.get("format") != ARTIFACT_FORMAT or raw.get("model_version") != MODEL_VERSION:
            raise PropensityLoadError("unknown artefact format or model version")
        if tuple(raw.get("feature_names", ())) != FEATURE_NAMES:
            raise PropensityLoadError("feature list differs from this code's FEATURE_NAMES")
        products = {UUID(key): _product_model(value) for key, value in raw["products"].items()}
    except PropensityLoadError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise PropensityLoadError(type(exc).__name__) from exc
    if frozenset(products) != catalogue_ids:
        raise PropensityLoadError("the model does not cover exactly the product catalogue")
    return PropensityModel(MODEL_VERSION, products)
