"""Relationship Manager outreach log (TASK-066, ADR-0022) and RM portfolio assignment (ADR-0032).

An RM records each call, meeting, email or note about a customer here so that the next RM sees what
was already said. Rows are append-only from the API: there is no edit or delete endpoint, because
the log is evidence of what the bank told the customer.
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from pbip_api.core.db import Base, created_at_col, in_check, str_enum, uuid_pk

MAX_NOTE_CHARS = 1000


class InteractionChannel(StrEnum):
    CALL = "CALL"
    MEETING = "MEETING"
    EMAIL = "EMAIL"
    NOTE = "NOTE"


class InteractionOutcome(StrEnum):
    INTERESTED = "INTERESTED"
    NOT_INTERESTED = "NOT_INTERESTED"
    FOLLOW_UP = "FOLLOW_UP"
    NO_ANSWER = "NO_ANSWER"
    INFORMATION_SENT = "INFORMATION_SENT"
    ACCOUNT_REVIEW = "ACCOUNT_REVIEW"


class RmInteraction(Base):
    __tablename__ = "rm_interactions"
    __table_args__ = (
        Index("ix_rm_interactions_customer_id_created_at", "customer_id", "created_at"),
        Index("ix_rm_interactions_staff_user_id", "staff_user_id"),
        in_check("channel", InteractionChannel, "channel_valid"),
        in_check("outcome", InteractionOutcome, "outcome_valid"),
        CheckConstraint(f"char_length(note) BETWEEN 1 AND {MAX_NOTE_CHARS}", name="note_length"),
    )

    id: Mapped[UUID] = uuid_pk()
    customer_id: Mapped[UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    # RESTRICT: a staff account with logged outreach is deactivated, never deleted, so the log
    # keeps naming who spoke to the customer.
    staff_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    channel: Mapped[InteractionChannel] = mapped_column(
        str_enum(InteractionChannel, 16), nullable=False
    )
    outcome: Mapped[InteractionOutcome] = mapped_column(
        str_enum(InteractionOutcome, 32), nullable=False
    )
    note: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class RmAssignment(Base):
    """One period during which an RM looks after a customer (ADR-0032).

    Reassigning or unassigning closes the active row and never deletes it, so the table is the
    history of who was responsible for the customer and who decided it. At most one row per
    customer is active (``unassigned_at IS NULL``), enforced by a partial unique index.
    """

    __tablename__ = "rm_assignments"
    __table_args__ = (
        Index(
            "uq_rm_assignments_active_customer_id",
            "customer_id",
            unique=True,
            postgresql_where=text("unassigned_at IS NULL"),
        ),
        Index("ix_rm_assignments_customer_id_assigned_at", "customer_id", "assigned_at"),
        Index("ix_rm_assignments_rm_user_id", "rm_user_id"),
        Index("ix_rm_assignments_assigned_by_user_id", "assigned_by_user_id"),
        Index("ix_rm_assignments_unassigned_by_user_id", "unassigned_by_user_id"),
        CheckConstraint(
            "unassigned_at IS NULL OR unassigned_at >= assigned_at",
            name="unassigned_after_assigned",
        ),
        CheckConstraint(
            "(unassigned_at IS NULL) = (unassigned_by_user_id IS NULL)", name="unassigned_by_set"
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    customer_id: Mapped[UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    # RESTRICT: staff are disabled, never deleted, so the history keeps naming who was responsible
    # and who decided it.
    rm_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    assigned_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    unassigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unassigned_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = created_at_col()
