"""Which wealth products suit the customer's RTQ-1 risk band (ADR-0058 section 9).

The rule is the assistant's (TASK-105): a product suits a band when its catalogue risk level is at
or below it (LOW < MODERATE < HIGH). Insurance is protection, not an investment, so it is never
rated. The band is the AUTHENTICATED customer's own, read through the customers service.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from pbip_api.modules.customers import service as customers_service
from pbip_api.modules.wealth.models import (
    InsuranceProduct,
    InvestmentInstrument,
    RiskLevel,
    WealthProduct,
)
from pbip_api.modules.wealth.schemas import SuitabilityItemOut, SuitabilityOut

_ORDER = {RiskLevel.LOW: 0, RiskLevel.MODERATE: 1, RiskLevel.HIGH: 2}
_LABEL = {RiskLevel.LOW: "Low", RiskLevel.MODERATE: "Moderate", RiskLevel.HIGH: "High"}
NO_BAND = "No risk profile recorded: answer the risk questionnaire on your profile to see this."
INSURANCE = "Insurance is protection, not an investment, so it is not matched to a risk band."


def _rated(
    product_id: UUID, kind: str, risk: RiskLevel, band: RiskLevel | None
) -> SuitabilityItemOut:
    if band is None:
        suitable, reason = None, NO_BAND
    elif _ORDER[risk] <= _ORDER[band]:
        suitable = True
        reason = f"{_LABEL[risk]} risk is within your {_LABEL[band].lower()} risk band."
    else:
        suitable = False
        reason = f"{_LABEL[risk]} risk is above your {_LABEL[band].lower()} risk band."
    return SuitabilityItemOut(
        product_id=product_id, kind=kind, risk_level=risk.value, suitable=suitable, reason=reason
    )


def suitability(session: Session, customer_id: UUID) -> SuitabilityOut:
    profile = customers_service.risk_profile(session, customer_id)
    band = RiskLevel(profile.band.value) if profile.band is not None else None
    items = [
        _rated(p.id, "WEALTH_PRODUCT", p.risk_level, band)
        for p in session.execute(select(WealthProduct).order_by(WealthProduct.code)).scalars()
    ]
    items += [
        _rated(i.id, "INSTRUMENT", i.risk_level, band)
        for i in session.execute(
            select(InvestmentInstrument).order_by(InvestmentInstrument.code)
        ).scalars()
    ]
    items += [
        SuitabilityItemOut(
            product_id=p.id, kind="INSURANCE", risk_level=None, suitable=None, reason=INSURANCE
        )
        for p in session.execute(select(InsuranceProduct).order_by(InsuranceProduct.code)).scalars()
    ]
    return SuitabilityOut(
        band=band.value if band is not None else None,
        questionnaire_version=profile.questionnaire_version,
        assessed_at=profile.assessed_at,
        items=items,
    )
