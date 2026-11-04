"""Service layer for personalized banking product recommendations (TASK-054).

Every input comes from the customer's own data: balances from the accounts module, the health score
and monthly income, spend and lifestyle share from analytics, and the weekly segment when one has
been assigned. Nothing is defaulted to a made-up value. Per ADR-0015 the engine gets only an age
band, from ``customers.service.recommendation_age_band`` (TASK-107): ``unknown`` unless the customer
has both a date of birth and a recorded consent, so the engine never sees a date of birth.

TASK-111 (ADR-0030): the engine also gets what the customer already holds, read through the owning
modules' service functions with the authenticated customer id: open accounts, active loans and
open fixed deposits. Only the kind and an amount go in, never ids or account numbers.

#343 (ADR-0054): the parts that are expensive to read (the entries pass behind the health score
and cash flow, loans and fixed deposits) are kept per customer in a version-checked in-process
cache. The version is read on every request in the same query as the accounts: every account's
row version, balance, hold and status, plus the dates the figures depend on. A posting, a hold, a
loan or deposit payment (each moves money on one of the accounts), or a new or closed account
changes it. The segment, the balances and the age band (date of birth AND consent) are read live
on every request, so withdrawing consent shows on the next response. A future change to loans or
deposits that does not post to one of the customer's accounts must add itself to ``_version``.
"""

from collections.abc import Hashable
from dataclasses import dataclass
from datetime import UTC, datetime
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
from pbip_api.core.versioned_cache import VersionedCache
from pbip_api.modules.accounts.models import Account, AccountStatus, AccountType
from pbip_api.modules.accounts.policy import list_owned_accounts_with_versions
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


def _held_accounts(accounts: list[Account]) -> list[HeldProduct]:
    """What the customer holds now: a closed account is no longer held."""
    return [
        HeldProduct(kind=_ACCOUNT_KINDS[account.account_type])
        for account in accounts
        if account.status != AccountStatus.CLOSED and account.account_type in _ACCOUNT_KINDS
    ]


def _held_loans_and_deposits(
    session: Session, customer_id: UUID, now: datetime
) -> tuple[HeldProduct, ...]:
    held = [  # list_loans returns ACTIVE loans only
        HeldProduct(kind=_LOAN_KINDS[loan.loan_type], amount_minor=loan.outstanding_principal_minor)
        for loan in loans_service.list_loans(session, customer_id)
    ]
    held += [  # MATURED is still held until the customer withdraws it
        HeldProduct(kind=HeldProductKind.FIXED_DEPOSIT, amount_minor=deposit.principal_minor)
        for deposit in wealth_service.list_fixed_deposits(session, customer_id, now)
        if deposit.status != "CLOSED"
    ]
    return tuple(held)


@dataclass(frozen=True, slots=True)
class _DerivedInputs:
    """The context fields that cost the entries pass and the loan and deposit queries."""

    health_score: int
    monthly_inflow_minor: int
    monthly_spend_minor: int
    savings_ratio: float
    discretionary_ratio: float
    buffer_months: float
    emi_ratio: float
    loans_and_deposits: tuple[HeldProduct, ...]


# ~1 KB per customer; the TTL only bounds memory, correctness comes from the version (ADR-0054).
_DERIVED = VersionedCache[UUID, _DerivedInputs](
    "recommendation_context", max_entries=10_000, ttl_seconds=900
)


def clear_context_cache() -> None:
    """For tests and benchmarks that need a cold cache."""
    _DERIVED.clear()


def _version(owned: list[tuple[Account, int]], now: datetime) -> Hashable:
    """Everything the derived inputs depend on besides the immutable ledger entries.

    Health and cash flow are per UTC calendar month; whether a deposit has matured is per bank day.
    Each account's row version changes with every posting and hold on it; balance, hold and status
    are in the key as well so the key never rests on the row version alone.
    """
    return (
        now.astimezone(UTC).date(),
        bank_today(now),
        tuple(
            (account.id, version, account.ledger_balance, account.held_amount, account.status)
            for account, version in owned
        ),
    )


def _derived_inputs(
    session: Session, customer_id: UUID, accounts: list[Account], now: datetime
) -> _DerivedInputs:
    # #343: the health score and the cash flow come from one pass over the entries.
    profile = analytics_service.get_customer_financial_profile(
        session, customer_id, now, accounts=accounts
    )
    if profile is None:  # unreachable: the caller passes at least one account
        msg = "a financial profile needs at least one account"
        raise ValueError(msg)
    health, cash = profile.health, profile.cash_flow
    return _DerivedInputs(
        health_score=health.overall_score,
        monthly_inflow_minor=cash.monthly_income_minor,
        monthly_spend_minor=cash.monthly_spend_minor,
        savings_ratio=health.components.savings_rate.metric_value,
        discretionary_ratio=cash.discretionary_ratio,
        buffer_months=health.components.buffer_months.metric_value,
        emi_ratio=health.components.emi_to_income.metric_value,
        loans_and_deposits=_held_loans_and_deposits(session, customer_id, now),
    )


def _context(
    session: Session, customer_id: UUID, now: datetime
) -> CustomerRecommendationContext | None:
    owned = list_owned_accounts_with_versions(session, customer_id)
    if not owned:
        return None
    accounts = [account for account, _version_of_row in owned]
    version = _version(owned, now)
    derived = _DERIVED.get(customer_id, version)
    if derived is None:
        derived = _derived_inputs(session, customer_id, accounts, now)
        _DERIVED.put(customer_id, version, derived)
    return CustomerRecommendationContext(
        customer_id=customer_id,
        segment=_segment(session, customer_id),
        health_score=derived.health_score,
        monthly_inflow_minor=derived.monthly_inflow_minor,
        monthly_spend_minor=derived.monthly_spend_minor,
        savings_ratio=derived.savings_ratio,
        discretionary_ratio=derived.discretionary_ratio,
        buffer_months=derived.buffer_months,
        emi_ratio=derived.emi_ratio,
        total_balance_minor=sum(account.available_balance for account in accounts),
        # ADR-0015: the band only, and "unknown" without a date of birth AND consent. Never cached.
        age_band=customers_service.recommendation_age_band(
            session, customer_id, bank_today(now)
        ).value,
        held_products=(*_held_accounts(accounts), *derived.loans_and_deposits),
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
