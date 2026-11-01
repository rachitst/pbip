"""Database access for RM insights: the outreach log it owns and the customer row it reads."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pbip_api.modules.auth.models import User
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.insights.models import (
    InteractionChannel,
    InteractionOutcome,
    RmInteraction,
)


def get_customer(session: Session, customer_id: UUID) -> Customer | None:
    return session.get(Customer, customer_id)


def add_interaction(
    session: Session,
    customer_id: UUID,
    staff_user_id: UUID,
    channel: InteractionChannel,
    outcome: InteractionOutcome,
    note: str,
) -> RmInteraction:
    row = RmInteraction(
        customer_id=customer_id,
        staff_user_id=staff_user_id,
        channel=channel,
        outcome=outcome,
        note=note,
    )
    session.add(row)
    session.flush()
    return row


def list_interactions(
    session: Session, customer_id: UUID, limit: int
) -> list[tuple[RmInteraction, str]]:
    """Newest first, each with the email of the staff member who logged it."""
    rows = session.execute(
        select(RmInteraction, User.email)
        .join(User, User.id == RmInteraction.staff_user_id)
        .where(RmInteraction.customer_id == customer_id)
        .order_by(RmInteraction.created_at.desc(), RmInteraction.id.desc())
        .limit(limit)
    ).all()
    return [(row[0], row[1]) for row in rows]


def count_interactions(session: Session, customer_id: UUID) -> int:
    return session.execute(
        select(func.count()).where(RmInteraction.customer_id == customer_id)
    ).scalar_one()


def staff_email(session: Session, user_id: UUID) -> str:
    return session.execute(select(User.email).where(User.id == user_id)).scalar_one()
