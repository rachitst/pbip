"""Product recommendations: eligibility rules, then a rule-based relevance ranking (not a model)."""

from pbip_ml.recommendations.contract import (
    BankingProduct,
    CustomerRecommendationContext,
    ProductType,
    Recommendation,
)
from pbip_ml.recommendations.engine import (
    PRODUCT_CATALOGUE,
    evaluate_eligibility,
    generate_recommendations,
)

__all__ = [
    "PRODUCT_CATALOGUE",
    "BankingProduct",
    "CustomerRecommendationContext",
    "ProductType",
    "Recommendation",
    "evaluate_eligibility",
    "generate_recommendations",
]
