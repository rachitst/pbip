"""ADR-0049 fail safe: the recommendation model is optional. No directory, a missing one, or a
model that did not pass the gate against rules-v1 all mean rules-v1, never a failed start."""

import json
from pathlib import Path

import pytest
from pbip_api.core.config import Settings
from pbip_api.modules.recommendations.scorer import load_propensity
from pbip_ml.recommendations import PRODUCT_CATALOGUE
from pbip_ml.recommendations.propensity import (
    FEATURE_NAMES,
    MANIFEST,
    MODEL_FILE,
    MODEL_VERSION,
    REPORT_FILE,
    ProductModel,
    PropensityModel,
    sha256_file,
)

pytestmark = pytest.mark.unit
N = len(FEATURE_NAMES)


def _settings(directory: str | None) -> Settings:
    return Settings(_env_file=None, recommendation_model_dir=directory)


def _write_model(out: Path, promoted: bool) -> PropensityModel:
    """The artefact layout ``training.write_artifacts`` produces, written by hand: the API tests
    must not import the training code (import-linter)."""
    model = PropensityModel(
        MODEL_VERSION,
        {
            p.product_id: ProductModel(-4.0, (0.1,) * N, (0.0,) * N, (1.0,) * N)
            for p in PRODUCT_CATALOGUE
        },
    )
    raw = {
        "format": "pbip-propensity-logreg-json-1",
        "model_version": MODEL_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "products": {
            str(pid): {"intercept": m.intercept, "coefficients": list(m.coefficients),
                       "means": list(m.means), "scales": list(m.scales)}
            for pid, m in model.products.items()
        },
    }  # fmt: skip
    (out / MODEL_FILE).write_text(json.dumps(raw), encoding="utf-8")
    (out / REPORT_FILE).write_text("{}", encoding="utf-8")
    manifest = {name: sha256_file(out / name) for name in (MODEL_FILE, REPORT_FILE)}
    (out / MANIFEST).write_text(json.dumps({**manifest, "promoted": promoted}), encoding="utf-8")
    return model


def test_no_directory_configured_means_rules_v1() -> None:
    assert load_propensity(_settings(None)) is None


def test_a_missing_directory_means_rules_v1_not_a_crash(tmp_path: Path) -> None:
    assert load_propensity(_settings(str(tmp_path / "absent"))) is None


def test_a_model_that_did_not_pass_the_gate_means_rules_v1(tmp_path: Path) -> None:
    _write_model(tmp_path, promoted=False)
    assert load_propensity(_settings(str(tmp_path))) is None


def test_a_promoted_intact_model_is_loaded(tmp_path: Path) -> None:
    model = _write_model(tmp_path, promoted=True)
    assert load_propensity(_settings(str(tmp_path))) == model


def test_a_missing_model_file_means_rules_v1(tmp_path: Path) -> None:
    """ADR-0053: the directory is mounted (docker-compose) before any model exists, or after a
    run whose model failed the gate; the report and manifest alone must not load anything."""
    _write_model(tmp_path, promoted=True)
    (tmp_path / MODEL_FILE).unlink()
    assert load_propensity(_settings(str(tmp_path))) is None
