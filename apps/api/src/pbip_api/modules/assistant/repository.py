"""Persistence for the assistant's conversation store (TASK-060)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from pbip_api.modules.assistant.models import MAX_STORED_CHARS, AssistantMessage
from pbip_api.modules.assistant.tools import ToolName


def add_exchange(
    session: Session, customer_id: UUID, message: str, reply: str, tool: ToolName | None
) -> None:
    """Store one (already redacted) question and its reply in the caller's transaction."""
    session.add_all(
        [
            AssistantMessage(
                customer_id=customer_id, role="user", content=message[:MAX_STORED_CHARS]
            ),
            AssistantMessage(
                customer_id=customer_id,
                role="assistant",
                content=reply[:MAX_STORED_CHARS],
                tool_name=tool.value if tool else None,
            ),
        ]
    )
    session.flush()


def list_messages(session: Session, customer_id: UUID, limit: int) -> list[AssistantMessage]:
    """The customer's own latest messages, oldest first."""
    rows = session.execute(
        select(AssistantMessage)
        .where(AssistantMessage.customer_id == customer_id)
        .order_by(AssistantMessage.created_at.desc(), AssistantMessage.role.asc())
        .limit(limit)
    ).scalars()
    return list(reversed(list(rows)))
