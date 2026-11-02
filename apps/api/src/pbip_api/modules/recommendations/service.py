"""Service layer for personalized banking product recommendations (TASK-054).

Every input comes from the customer's own data: balances from the accounts module, the health score
and monthly income, spend and lifestyle share from analytics, and the weekly segment when one has
been assigned. Nothing is defaulted to a made-up value. Per ADR-0015 the age band stays "unknown"
until consented date of birth exists (TASK-107), so the engine never sees a date of birth.
"""

from datetime import datetime
from uuid import UUID

from pbip_ml.recommendations import CustomerRecommendationContext, generate_recommendations
from pbip_ml.recommendations.engine import MODEL_VERSION
from sqlalchemy.orm import Session

from pbip_api.core.db import utcnow
from pbip_api.core.errors import NotFoundError
from pbip_api.modules.accounts import service as accounts_service
from pbip_api.modules.analytics import service as analytics_service
from pbip_api.modules.recommendations.schemas import (
    ProductRecommendationItem,
    RecommendationsResponse,
)

NO_SEGMENT = "UNSEGMENTED"  # targeted by no product: segment matching simply does not apply


def _segment(session: Session, customer_id: UUID) -> str:
    try:
        return analytics_service.get_customer_segment(session, customer_id).segment
    except NotFoundError:
        return NO_SEGMENT


def _context(
    session: Session, customer_id: UUID, now: datetime
) -> CustomerRecommendationContext | None:
    accounts = accounts_service.list_accounts(session, customer_id)
    if not accounts:
        return None
    primary = accounts[0]
    health = analytics_service.get_financial_health_score(session, customer_id, primary.id, now)
    cash = analytics_service.get_cash_flow_profile(session, customer_id, primary.id, now)
    return CustomerRecommendationContext(
        customer_id=customer_id,
        segment=_segment(session, customer_id),
        health_score=health.overall_score,
        monthly_inflow_minor=cash.monthly_income_minor,
        monthly_spend_minor=cash.monthly_spend_minor,
        savings_ratio=health.components.savings_rate.metric_value,
        discretionary_ratio=cash.discretionary_ratio,
        buffer_months=health.components.buffer_months.metric_value,
        emi_ratio=health.components.emi_to_income.metric_value,
        total_balance_minor=sum(account.available_balance for account in accounts),
        age_band="unknown",  # ADR-0015: no consented date of birth yet (TASK-107)
    )


def get_customer_recommendations(
    session: Session, customer_id: UUID, now: datetime | None = None, top_k: int = 3
) -> RecommendationsResponse:
    """Top ``top_k`` eligible products for the customer, ordered by the rule-based score.

    The customer route keeps the default of 3; the RM insights view (TASK-066) asks for every
    eligible product so it can find the best loan product even when it is not in the top 3.
    """
    eval_time = now or utcnow()
    ctx = _context(session, customer_id, eval_time)
    recommendations = [] if ctx is None else generate_recommendations(ctx, top_k=top_k)
    return RecommendationsResponse(
        customer_id=customer_id,
        recommendations=[
            ProductRecommendationItem(
                product_id=rec.product_id,
                product_type=rec.product_type.value,
                title=rec.title,
                description=rec.description,
                score=rec.score,
                reasons=rec.reasons,
                model_version=rec.model_version,
                cta_label=rec.cta_label,
            )
            for rec in recommendations
        ],
        model_version=MODEL_VERSION,
        evaluated_at=eval_time,
    )
