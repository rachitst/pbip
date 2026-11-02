"""Allow the get_unusual_expenses assistant tool in the conversation store (TASK-110).

``assistant_messages.tool_name`` is checked against the approved tool registry; the new read-only
tool joins the list. Downgrade: the old list cannot hold the new name, so stored replies of that
tool keep their (redacted) text and lose only the tool name.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "ck_assistant_messages_tool_name_valid"
_OLD = (
    "'get_account_summary', 'get_recent_transactions', 'get_spend', 'get_balance_forecast', "
    "'get_loan_affordability'"
)
_NEW = _OLD + ", 'get_unusual_expenses'"


def _replace(tools: str) -> None:
    op.drop_constraint(op.f(_NAME), "assistant_messages", type_="check")
    op.create_check_constraint(
        op.f(_NAME), "assistant_messages", f"tool_name IS NULL OR tool_name IN ({tools})"
    )


def upgrade() -> None:
    _replace(_NEW)


def downgrade() -> None:
    op.execute(
        "UPDATE assistant_messages SET tool_name = NULL WHERE tool_name = 'get_unusual_expenses'"
    )
    _replace(_OLD)
