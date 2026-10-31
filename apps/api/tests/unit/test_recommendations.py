"""Unit tests for product recommendations service and schemas (TASK-054).

Verifies:
- Schema validation for product items and response payloads
- Handling empty reasons or invalid scores
"""

from uuid import uuid4

import pytest
from pbip_api.modules.recommendations.schemas import (
    ProductRecommendationItem,
)
from pydantic import ValidationError

pytestmark = pytest.mark.unit


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
        model_version="rules-v1",
        cta_label="Open",
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
            model_version="rules-v1",
            cta_label="Open",
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
            model_version="rules-v1",
            cta_label="Open",
        )
