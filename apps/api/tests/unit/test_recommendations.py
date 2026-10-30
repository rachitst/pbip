"""Unit tests for product recommendations service and schemas (TASK-054).

Verifies:
- ADR-0015 age band derivation
- Schema validation for product items and response payloads
- Handling empty reasons or invalid scores
"""

from datetime import date
from uuid import uuid4

import pytest
from pbip_api.modules.recommendations.schemas import (
    ProductRecommendationItem,
)
from pbip_api.modules.recommendations.service import derive_age_band
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def test_derive_age_band_per_adr_0015() -> None:
    """Verifies age band calculation across standard boundaries."""
    ref = date(2026, 10, 30)

    assert derive_age_band(None, ref) == "unknown"
    assert derive_age_band(date(2010, 10, 30), ref) == "<18"  # 16 years
    assert derive_age_band(date(2005, 10, 30), ref) == "18-25"  # 21 years
    assert derive_age_band(date(1995, 5, 15), ref) == "26-35"  # 31 years
    assert derive_age_band(date(1985, 1, 1), ref) == "36-50"  # 41 years
    assert derive_age_band(date(1972, 8, 20), ref) == "51-59"  # 54 years
    assert derive_age_band(date(1960, 4, 10), ref) == "60+"  # 66 years


def test_product_recommendation_item_validation() -> None:
    """Valid item passes; missing reasons or out-of-bounds score fail."""
    valid_id = uuid4()
    item = ProductRecommendationItem(
        product_id=valid_id,
        product_type="SAVINGS",
        title="Savings Accelerator",
        description="High interest savings.",
        score=0.85,
        reasons=["High savings rate."],
        model_version="rec-v1.0.0",
        cta_label="Open",
        cta_path="/open",
    )
    assert item.product_id == valid_id
    assert item.score == 0.85

    # Empty reasons must fail
    with pytest.raises(ValidationError):
        ProductRecommendationItem(
            product_id=valid_id,
            product_type="SAVINGS",
            title="Savings Accelerator",
            description="High interest savings.",
            score=0.85,
            reasons=[],
            model_version="rec-v1.0.0",
            cta_label="Open",
            cta_path="/open",
        )

    # Score > 1.0 must fail
    with pytest.raises(ValidationError):
        ProductRecommendationItem(
            product_id=valid_id,
            product_type="SAVINGS",
            title="Savings Accelerator",
            description="High interest savings.",
            score=1.5,
            reasons=["Reason"],
            model_version="rec-v1.0.0",
            cta_label="Open",
            cta_path="/open",
        )
