"""Object-level authorization for fixed deposits (TASK-064) and investment holdings (ADR-0058).

The ONLY way application code obtains a deposit or a holding on behalf of a customer. Someone
else's and one that does not exist are indistinguishable (404), so ids cannot be probed.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from pbip_api.core.errors import NotFoundError
from pbip_api.modules.wealth.models import (
    FixedDeposit,
    FundSip,
    InsuranceApplication,
    InvestmentHolding,
)


def get_owned_fixed_deposit(
    session: Session, customer_id: UUID, deposit_id: UUID, *, for_update: bool = False
) -> FixedDeposit:
    stmt = select(FixedDeposit).where(
        FixedDeposit.id == deposit_id, FixedDeposit.customer_id == customer_id
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    deposit = session.execute(stmt).scalar_one_or_none()
    if deposit is None:
        raise NotFoundError("fixed deposit not found")
    return deposit


def list_owned_fixed_deposits(session: Session, customer_id: UUID) -> list[FixedDeposit]:
    stmt = (
        select(FixedDeposit)
        .where(FixedDeposit.customer_id == customer_id)
        .order_by(FixedDeposit.created_at.desc(), FixedDeposit.id.desc())
    )
    return list(session.execute(stmt).scalars())


def get_owned_holding(
    session: Session, customer_id: UUID, holding_id: UUID, *, for_update: bool = False
) -> InvestmentHolding:
    stmt = select(InvestmentHolding).where(
        InvestmentHolding.id == holding_id, InvestmentHolding.customer_id == customer_id
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    holding = session.execute(stmt).scalar_one_or_none()
    if holding is None:
        raise NotFoundError("holding not found")
    return holding


def list_owned_holdings(session: Session, customer_id: UUID) -> list[InvestmentHolding]:
    """The customer's open positions (a fully sold holding keeps its row at zero units)."""
    stmt = (
        select(InvestmentHolding)
        .where(InvestmentHolding.customer_id == customer_id, InvestmentHolding.units_micro > 0)
        .order_by(InvestmentHolding.created_at, InvestmentHolding.id)
    )
    return list(session.execute(stmt).scalars())


def get_owned_sip(
    session: Session, customer_id: UUID, sip_id: UUID, *, for_update: bool = False
) -> FundSip:
    stmt = select(FundSip).where(FundSip.id == sip_id, FundSip.customer_id == customer_id)
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    sip = session.execute(stmt).scalar_one_or_none()
    if sip is None:
        raise NotFoundError("SIP not found")
    return sip


def list_owned_sips(session: Session, customer_id: UUID) -> list[FundSip]:
    stmt = (
        select(FundSip)
        .where(FundSip.customer_id == customer_id)
        .order_by(FundSip.created_at.desc(), FundSip.id.desc())
    )
    return list(session.execute(stmt).scalars())


def get_owned_insurance_application(
    session: Session, customer_id: UUID, application_id: UUID
) -> InsuranceApplication:
    application = session.execute(
        select(InsuranceApplication).where(
            InsuranceApplication.id == application_id,
            InsuranceApplication.customer_id == customer_id,
        )
    ).scalar_one_or_none()
    if application is None:
        raise NotFoundError("insurance application not found")
    return application


def list_owned_insurance_applications(
    session: Session, customer_id: UUID
) -> list[InsuranceApplication]:
    stmt = (
        select(InsuranceApplication)
        .where(InsuranceApplication.customer_id == customer_id)
        .order_by(InsuranceApplication.created_at.desc(), InsuranceApplication.id.desc())
    )
    return list(session.execute(stmt).scalars())
