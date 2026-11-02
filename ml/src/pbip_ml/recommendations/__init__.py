"""Product recommendations: eligibility rules, then a rule-based relevance ranking (not a model)."""

from pbip_ml.recommendations.contract import (
    BankingProduct,
    CustomerRecommendationContext,
    HeldProduct,
    HeldProductKind,
    ProductType,
    Recommendation,
)
from pbip_ml.recommendations.engine import (
    PRODUCT_CATALOGUE,
    evaluate_eligibility,
    generate_recommendations,
    held_product_rule,
)

__all__ = [
    "PRODUCT_CATALOGUE",
    "BankingProduct",
    "CustomerRecommendationContext",
    "HeldProduct",
    "HeldProductKind",
    "ProductType",
    "Recommendation",
    "evaluate_eligibility",
    "generate_recommendations",
    "held_product_rule",
]
