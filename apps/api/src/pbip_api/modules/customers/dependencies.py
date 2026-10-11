from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import get_session
from pbip_api.core.errors import ForbiddenError
from pbip_api.modules.auth.dependencies import Principal, get_current_principal
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.customers.service import CustomerService


def get_current_customer(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_session),
) -> Customer:
    """Resolve the authenticated user's customer profile. Staff users have none -> 403.

    Customer-scoped endpoints take their customer id from HERE, never from the request.
    """
    customer = session.execute(
        select(Customer).where(Customer.user_id == principal.user_id)
    ).scalar_one_or_none()
    if customer is None:
        raise ForbiddenError("this endpoint is for customers only")
    return customer


def get_customer_service(
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> CustomerService:
    return CustomerService(session, settings)
