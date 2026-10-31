"""Conversation store for the assistant (TASK-060): per customer, redacted, no cross-user index.

Only the customer's own messages and the server-written replies are kept, after the PII
redactor (TASK-059) has masked account numbers, tokens and emails. Tool results are not stored;
the tool name is, so a conversation can be audited alongside the audit log.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from pbip_api.core.db import Base, created_at_col, uuid_pk

MAX_STORED_CHARS = 2000


class AssistantMessage(Base):
    __tablename__ = "assistant_messages"
    __table_args__ = (
        Index("ix_assistant_messages_customer_id_created_at", "customer_id", "created_at"),
        CheckConstraint("role IN ('user', 'assistant')", name="role_valid"),
        CheckConstraint(f"char_length(content) <= {MAX_STORED_CHARS}", name="content_length"),
        CheckConstraint(
            "tool_name IS NULL OR tool_name IN ('get_account_summary', 'get_recent_transactions', "
            "'get_spend', 'get_balance_forecast', 'get_loan_affordability')",
            name="tool_name_valid",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    customer_id: Mapped[UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = created_at_col()
