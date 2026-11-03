"""Which ordering the recommendation engine uses: rules-v1, or the promoted simulated-uptake
propensity model (ADR-0049).

The model loads once per process, from ``RECOMMENDATION_MODEL_DIR`` (operator-controlled,
read-only in Docker), never on a request. It is plain JSON checked against a SHA-256 manifest
that the training gate writes only for a model that beat rules-v1 on the simulated test months.
Anything wrong (no directory configured, files missing, altered, or a model that did not pass the
gate) logs ``recommendation_model_not_loaded`` and the API serves rules-v1: fail safe.
"""

from functools import lru_cache
from pathlib import Path

import structlog
from pbip_ml.recommendations import PRODUCT_CATALOGUE
from pbip_ml.recommendations.propensity import PropensityLoadError, PropensityModel, load

from pbip_api.core.config import Settings, get_settings

_logger = structlog.get_logger(__name__)


def load_propensity(settings: Settings) -> PropensityModel | None:
    """``None`` means rules-v1 orders the products."""
    if not settings.recommendation_model_dir:
        _logger.info(
            "recommendation_model_not_loaded", reason="RECOMMENDATION_MODEL_DIR is not set"
        )
        return None
    ids = frozenset(product.product_id for product in PRODUCT_CATALOGUE)
    try:
        model = load(Path(settings.recommendation_model_dir), ids)
    except PropensityLoadError as exc:
        _logger.warning("recommendation_model_not_loaded", reason=str(exc)[:200])
        return None
    _logger.info("recommendation_model_loaded", model_version=model.version)
    return model


@lru_cache(maxsize=1)
def active_propensity() -> PropensityModel | None:
    """The process-wide model, loaded once; ``main`` calls it at startup, not on a request."""
    return load_propensity(get_settings())
