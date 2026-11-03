"""Database access for RM insights: the outreach log and RM assignments it owns, and the customer
and staff rows it reads."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, func, select, text
from sqlalchemy.orm import Session

from pbip_api.modules.auth.models import RoleName, User, UserRole, UserStatus
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.insights.models import (
    InteractionChannel,
    InteractionOutcome,
    RmAssignment,
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


def lock_customer_assignment(session: Session, customer_id: UUID) -> None:
    """Serialise assignment changes of one customer until the transaction ends (ADR-0032).

    The two-key form lives in a key space of its own, so it never collides with the single-key
    per-user and per-customer locks of the payment and account flows.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('rm_assignments'), hashtext(:cid))"),
        {"cid": str(customer_id)},
    )


def active_assignment(session: Session, customer_id: UUID) -> tuple[RmAssignment, str] | None:
    """The customer's current assignment with the RM's email, or None when unassigned."""
    row = session.execute(
        select(RmAssignment, User.email)
        .join(User, User.id == RmAssignment.rm_user_id)
        .where(RmAssignment.customer_id == customer_id, RmAssignment.unassigned_at.is_(None))
    ).first()
    return None if row is None else (row[0], row[1])


def is_assigned(session: Session, customer_id: UUID, rm_user_id: UUID) -> bool:
    return (
        session.execute(
            select(RmAssignment.id).where(
                RmAssignment.customer_id == customer_id,
                RmAssignment.rm_user_id == rm_user_id,
                RmAssignment.unassigned_at.is_(None),
            )
        ).first()
        is not None
    )


def assigned_customer_ids(session: Session, rm_user_id: UUID) -> set[UUID]:
    return set(
        session.scalars(
            select(RmAssignment.customer_id).where(
                RmAssignment.rm_user_id == rm_user_id, RmAssignment.unassigned_at.is_(None)
            )
        )
    )


def add_assignment(
    session: Session, customer_id: UUID, rm_user_id: UUID, assigned_by: UUID, at: datetime
) -> RmAssignment:
    row = RmAssignment(
        customer_id=customer_id,
        rm_user_id=rm_user_id,
        assigned_by_user_id=assigned_by,
        assigned_at=at,
    )
    session.add(row)
    session.flush()
    return row


def close_assignment(
    session: Session, row: RmAssignment, unassigned_by: UUID, at: datetime
) -> None:
    row.unassigned_at = max(at, row.assigned_at)  # never before it started (CHECK constraint)
    row.unassigned_by_user_id = unassigned_by
    session.flush()


def _active_rms() -> Select[UUID, str]:
    return (
        select(User.id, User.email)
        .join(UserRole, UserRole.user_id == User.id)
        .where(
            UserRole.role_name == RoleName.RELATIONSHIP_MANAGER.value,
            User.status == UserStatus.ACTIVE,
        )
    )


def active_relationship_manager(session: Session, user_id: UUID) -> str | None:
    """The email of an ACTIVE user holding the RM role, or None."""
    row = session.execute(_active_rms().where(User.id == user_id)).first()
    return None if row is None else row[1]


def active_relationship_managers(session: Session) -> list[tuple[UUID, str]]:
    rows = session.execute(_active_rms().order_by(User.email)).all()
    return [(row[0], row[1]) for row in rows]
