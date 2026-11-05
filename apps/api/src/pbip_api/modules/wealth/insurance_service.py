"""Simulated insurance quotes and applications (ADR-0058 section 8).

A quote reads only the catalogue row and what the customer declares in the request (cover, term,
age band with explicit consent); nothing is read from the profile, so the ADR-0015 consent for
recommendations is never reused for pricing. An application stores the quote and its status by
the deterministic rules in ``wealth/insurance.py``. No policy is issued and no premium is debited:
nothing here touches the ledger.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings
from pbip_api.core.errors import NotFoundError, UnprocessableError
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.customers.demographics import AgeBand
from pbip_api.modules.wealth import insurance
from pbip_api.modules.wealth.models import (
    InsuranceApplication,
    InsuranceProduct,
    InsuranceType,
)
from pbip_api.modules.wealth.policy import (
    get_owned_insurance_application,
    list_owned_insurance_applications,
)
from pbip_api.modules.wealth.schemas import (
    InsuranceApplicationOut,
    InsuranceQuoteOut,
    InsuranceQuoteRequest,
)
from pbip_api.modules.wealth.service import (
    WealthResult,
    _begin_idempotent,
    _hash,
    _lock_user,
    _store_response,
)


def _product(session: Session, product_id: UUID) -> InsuranceProduct:
    product = session.get(InsuranceProduct, product_id)
    if product is None:
        raise NotFoundError("insurance product not found")
    return product


def _rules(product: InsuranceProduct) -> insurance.PlanRules:
    return insurance.PlanRules(
        term_life=product.insurance_type == InsuranceType.TERM_LIFE,
        indicative_premium_minor=product.indicative_annual_premium_minor,
        reference_cover_minor=product.reference_cover_minor,
        reference_age=product.reference_age,
        reference_term_years=product.reference_term_years,
        min_entry_age=product.min_entry_age,
        max_entry_age=product.max_entry_age,
        max_cover_age=product.max_cover_age,
    )


def _decide(product: InsuranceProduct, request: InsuranceQuoteRequest) -> insurance.QuoteDecision:
    """Validate the terms against the product (422), then apply the status rules."""
    if not product.min_cover_minor <= request.cover_minor <= product.max_cover_minor:
        raise UnprocessableError("cover is outside this plan's range")
    if request.term_years not in product.policy_terms_years:
        raise UnprocessableError("this policy term is not offered for this plan")
    return insurance.decide(
        _rules(product), request.cover_minor, request.term_years, AgeBand(request.age_band)
    )


def quote(session: Session, request: InsuranceQuoteRequest) -> InsuranceQuoteOut:
    """What applying with these terms would record. Stores nothing."""
    product = _product(session, request.product_id)
    decision = _decide(product, request)
    return InsuranceQuoteOut(
        product_id=product.id,
        product_name=product.name,
        cover_minor=request.cover_minor,
        term_years=request.term_years,
        age_band=request.age_band,
        annual_premium_minor=decision.annual_premium_minor,
        outcome=decision.status.value,
        reason=decision.reason,
        age_factor_bps=decision.age_factor_bps,
        term_factor_bps=decision.term_factor_bps,
        quote_version=insurance.QUOTE_VERSION,
    )


def _application_out(
    application: InsuranceApplication, product_name: str
) -> InsuranceApplicationOut:
    return InsuranceApplicationOut(
        id=application.id,
        product_id=application.product_id,
        product_name=product_name,
        cover_minor=application.cover_minor,
        term_years=application.term_years,
        age_band=application.age_band,
        annual_premium_minor=application.annual_premium_minor,
        status=application.status.value,
        decision_reason=application.decision_reason,
        quote_version=application.quote_version,
        created_at=application.created_at,
    )


def _names(session: Session, ids: set[UUID]) -> dict[UUID, str]:
    if not ids:
        return {}
    rows = session.execute(
        select(InsuranceProduct.id, InsuranceProduct.name).where(InsuranceProduct.id.in_(ids))
    ).all()
    return {row.id: row.name for row in rows}


def list_applications(session: Session, customer_id: UUID) -> list[InsuranceApplicationOut]:
    applications = list_owned_insurance_applications(session, customer_id)
    names = _names(session, {a.product_id for a in applications})
    return [_application_out(a, names[a.product_id]) for a in applications]


def get_application(
    session: Session, customer_id: UUID, application_id: UUID
) -> InsuranceApplicationOut:
    application = get_owned_insurance_application(session, customer_id, application_id)
    return _application_out(
        application, _names(session, {application.product_id})[application.product_id]
    )


def apply(
    session: Session,
    settings: Settings,
    *,
    customer_id: UUID,
    user_id: UUID,
    request: InsuranceQuoteRequest,
    idempotency_key: str,
    meta: RequestMeta,
) -> WealthResult:
    """Record an application with its quote and status (one transaction, audited, idempotent)."""
    _lock_user(session, user_id)
    request_hash = _hash(
        {"op": "wealth.insurance.apply"} | request.model_dump(mode="json"), user_id
    )
    idem_id, replay = _begin_idempotent(session, settings, user_id, idempotency_key, request_hash)
    if replay is not None:
        return replay

    product = _product(session, request.product_id)
    decision = _decide(product, request)
    application = InsuranceApplication(
        customer_id=customer_id,
        user_id=user_id,
        product_id=product.id,
        cover_minor=request.cover_minor,
        term_years=request.term_years,
        age_band=request.age_band,
        annual_premium_minor=decision.annual_premium_minor,
        status=decision.status,
        decision_reason=decision.reason,
        quote_version=insurance.QUOTE_VERSION,
    )
    session.add(application)
    session.flush()
    session.refresh(application)
    AuditService(session).record(
        user_id,
        "INSURANCE_APPLICATION_SUBMITTED",
        "insurance_application",
        str(application.id),
        ip=meta.ip,
        request_id=meta.request_id,
        details={
            "product_id": str(product.id),
            "cover_minor": application.cover_minor,
            "term_years": application.term_years,
            "status": application.status.value,
            "quote_version": application.quote_version,
            "age_band_consent": True,
        },
    )
    outcome = WealthResult(201, _application_out(application, product.name).model_dump(mode="json"))
    _store_response(session, idem_id, outcome, None)
    session.commit()
    return outcome
