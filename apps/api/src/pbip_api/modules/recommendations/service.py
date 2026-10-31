"""Service layer for personalized banking product recommendations (TASK-054)."""

from datetime import UTC, date, datetime
from uuid import UUID

from pbip_ml.recommendations import (
    CustomerRecommendationContext,
    generate_recommendations,
)
from pbip_ml.recommendations.engine import MODEL_VERSION
from sqlalchemy.orm import Session

from pbip_api.core.db import utcnow
from pbip_api.core.errors import DomainError, NotFoundError
from pbip_api.modules.accounts import service as accounts_service
from pbip_api.modules.analytics import service as analytics_service
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.recommendations.schemas import (
    ProductRecommendationItem,
    RecommendationsResponse,
)


def derive_age_band(dob: date | None, today: date | None = None) -> str:
    """Derive customer age band per ADR-0015 (18-25, 26-35, 36-50, 51-59, 60+, or unknown)."""
    if dob is None:
        return "unknown"
    ref = today or datetime.now(UTC).date()
    age = ref.year - dob.year - ((ref.month, ref.day) < (dob.month, dob.day))
    if age < 18:
        return "<18"
    if 18 <= age <= 25:
        return "18-25"
    if 26 <= age <= 35:
        return "26-35"
    if 36 <= age <= 50:
        return "36-50"
    if 51 <= age <= 59:
        return "51-59"
    return "60+"


def get_customer_recommendations(
    session: Session,
    customer_id: UUID,
    now: datetime | None = None,
) -> RecommendationsResponse:
    """Evaluate eligibility and propensity ranking for the authenticated customer."""
    eval_time = now or utcnow()
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise NotFoundError("customer not found")

    accounts = accounts_service.list_accounts(session, customer_id)
    total_balance = sum(acc.available_balance for acc in accounts)

    # Behavioral segment lookup
    segment_name = "BUDGET_CONSCIOUS"
    try:
        seg_record = analytics_service.get_customer_segment(session, customer_id)
        segment_name = seg_record.segment
    except NotFoundError:
        pass

    # Health & cash flow metrics
    health_score = 50
    savings_ratio = 0.0
    buffer_months = 0.0
    emi_ratio = 0.0
    discretionary_ratio = 0.0
    monthly_inflow = 0
    monthly_spend = 0

    if accounts:
        primary_acc = accounts[0]
        try:
            health = analytics_service.get_financial_health_score(
                session, customer_id, primary_acc.id, now=eval_time
            )
            health_score = health.overall_score
            savings_ratio = health.components.savings_rate.metric_value
            buffer_months = health.components.buffer_months.metric_value
            emi_ratio = health.components.emi_to_income.metric_value
        except (DomainError, ValueError):
            pass

        # Trailing monthly debits & credits estimate from balance and buffer
        if buffer_months > 0 and total_balance > 0:
            monthly_spend = round(total_balance / buffer_months)
        else:
            monthly_spend = max(total_balance // 2, 0)

        if savings_ratio < 1.0:
            monthly_inflow = round(monthly_spend / max(1.0 - savings_ratio, 0.1))
        else:
            monthly_inflow = monthly_spend * 2

        if segment_name == "DISCRETIONARY_SPENDER":
            discretionary_ratio = 0.55
        elif segment_name == "HIGH_NET_WORTH":
            discretionary_ratio = 0.35
        elif segment_name == "BUDGET_CONSCIOUS":
            discretionary_ratio = 0.10
        else:
            discretionary_ratio = 0.25

    age_band = derive_age_band(customer.date_of_birth, eval_time.date())

    ctx = CustomerRecommendationContext(
        customer_id=customer_id,
        segment=segment_name,
        health_score=health_score,
        monthly_inflow_minor=monthly_inflow,
        monthly_spend_minor=monthly_spend,
        savings_ratio=savings_ratio,
        discretionary_ratio=discretionary_ratio,
        buffer_months=buffer_months,
        emi_ratio=emi_ratio,
        total_balance_minor=total_balance,
        age_band=age_band,
    )

    ml_recommendations = generate_recommendations(ctx, top_k=3, model_version=MODEL_VERSION)

    items = [
        ProductRecommendationItem(
            product_id=rec.product_id,
            product_type=rec.product_type.value,
            title=rec.title,
            description=rec.description,
            score=rec.score,
            reasons=rec.reasons,
            model_version=rec.model_version,
            cta_label=rec.cta_label,
            cta_path=rec.cta_path,
        )
        for rec in ml_recommendations
    ]

    return RecommendationsResponse(
        customer_id=customer_id,
        recommendations=items,
        model_version=MODEL_VERSION,
        evaluated_at=eval_time,
    )
