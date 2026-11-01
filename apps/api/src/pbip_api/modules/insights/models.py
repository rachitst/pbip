"""Relationship Manager outreach log (TASK-066, ADR-0021).

An RM records each call, meeting, email or note about a customer here so that the next RM sees what
was already said. Rows are append-only from the API: there is no edit or delete endpoint, because
the log is evidence of what the bank told the customer.
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text
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
