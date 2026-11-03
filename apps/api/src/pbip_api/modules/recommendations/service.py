"""Service layer for personalized banking product recommendations (TASK-054).

Every input comes from the customer's own data: balances from the accounts module, the health score
and monthly income, spend and lifestyle share from analytics, and the weekly segment when one has
been assigned. Nothing is defaulted to a made-up value. Per ADR-0015 the engine gets only an age
band, from ``customers.service.recommendation_age_band`` (TASK-107): ``unknown`` unless the customer
has both a date of birth and a recorded consent, so the engine never sees a date of birth.

TASK-111 (ADR-0030): the engine also gets what the customer already holds, read through the owning
modules' service functions with the authenticated customer id: open accounts, active loans and
open fixed deposits. Only the kind and an amount go in, never ids or account numbers.
"""

from datetime import datetime
from uuid import UUID

from pbip_ml.recommendations import (
    CustomerRecommendationContext,
    HeldProduct,
    HeldProductKind,
    generate_recommendations,
)
from pbip_ml.recommendations.engine import MODEL_VERSION, SCORE_LABEL
from pbip_ml.recommendations.propensity import SCORE_LABEL as SIMULATED_SCORE_LABEL
from pbip_ml.recommendations.propensity import PropensityModel
from sqlalchemy.orm import Session

from pbip_api.core.calendar import bank_today
from pbip_api.core.db import utcnow
from pbip_api.core.errors import NotFoundError
from pbip_api.modules.accounts.models import Account, AccountStatus, AccountType
from pbip_api.modules.analytics import service as analytics_service
from pbip_api.modules.customers import service as customers_service
from pbip_api.modules.loans import service as loans_service
from pbip_api.modules.recommendations.schemas import (
    ProductRecommendationItem,
    RecommendationsResponse,
)
from pbip_api.modules.recommendations.scorer import active_propensity
from pbip_api.modules.wealth import service as wealth_service

NO_SEGMENT = "UNSEGMENTED"  # targeted by no product: segment matching simply does not apply


def _segment(session: Session, customer_id: UUID) -> str:
    try:
        return analytics_service.get_customer_segment(session, customer_id).segment
    except NotFoundError:
        return NO_SEGMENT


_ACCOUNT_KINDS = {
    AccountType.SAVINGS: HeldProductKind.SAVINGS_ACCOUNT,
    AccountType.CURRENT: HeldProductKind.CURRENT_ACCOUNT,
}
_LOAN_KINDS = {
    "PERSONAL": HeldProductKind.PERSONAL_LOAN,
    "AUTO": HeldProductKind.AUTO_LOAN,
    "HOME": HeldProductKind.HOME_LOAN,
}


def _held_products(
    session: Session, customer_id: UUID, accounts: list[Account], now: datetime
) -> tuple[HeldProduct, ...]:
    """What the customer holds now: a closed account or deposit is no longer held."""
    held = [
        HeldProduct(kind=_ACCOUNT_KINDS[account.account_type])
        for account in accounts
        if account.status != AccountStatus.CLOSED and account.account_type in _ACCOUNT_KINDS
    ]
    held += [  # list_loans returns ACTIVE loans only
        HeldProduct(kind=_LOAN_KINDS[loan.loan_type], amount_minor=loan.outstanding_principal_minor)
        for loan in loans_service.list_loans(session, customer_id)
    ]
    held += [  # MATURED is still held until the customer withdraws it
        HeldProduct(kind=HeldProductKind.FIXED_DEPOSIT, amount_minor=deposit.principal_minor)
        for deposit in wealth_service.list_fixed_deposits(session, customer_id, now)
        if deposit.status != "CLOSED"
    ]
    return tuple(held)


def _context(
    session: Session, customer_id: UUID, now: datetime
) -> CustomerRecommendationContext | None:
    # #343: the accounts, the health score and the cash flow come from two queries in total.
    profile = analytics_service.get_customer_financial_profile(session, customer_id, now)
    if profile is None:
        return None
    accounts, health, cash = profile.accounts, profile.health, profile.cash_flow
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
        # ADR-0015: the band only, and "unknown" without a date of birth AND consent.
        age_band=customers_service.recommendation_age_band(
            session, customer_id, bank_today(now)
        ).value,
        held_products=_held_products(session, customer_id, accounts, now),
    )


_ACTIVE = object()  # sentinel: use the process-wide model (``scorer.active_propensity``)


def get_customer_recommendations(
    session: Session,
    customer_id: UUID,
    now: datetime | None = None,
    top_k: int = 3,
    propensity: PropensityModel | object | None = _ACTIVE,
) -> RecommendationsResponse:
    """Top ``top_k`` eligible products for the customer, ordered by rules-v1, or by the promoted
    simulated-uptake model when one is loaded (ADR-0049). The rules always decide eligibility.

    The customer route keeps the default of 3; the RM insights view (TASK-066) asks for every
    eligible product so it can find the best loan product even when it is not in the top 3.
    """
    eval_time = now or utcnow()
    model = active_propensity() if propensity is _ACTIVE else propensity
    model = model if isinstance(model, PropensityModel) else None
    ctx = _context(session, customer_id, eval_time)
    recommendations = (
        [] if ctx is None else generate_recommendations(ctx, top_k=top_k, propensity=model)
    )
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
        model_version=MODEL_VERSION if model is None else model.version,
        score_version=SCORE_LABEL if model is None else SIMULATED_SCORE_LABEL,
        score_from_simulated_data=model is not None,
        evaluated_at=eval_time,
    )
