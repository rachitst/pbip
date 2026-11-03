"""Allow the FAQ, product and investment-suggestion assistant tools in the store (TASK-105).

``assistant_messages.tool_name`` is checked against the approved tool registry; the three new
read-only tools join the list. Downgrade: the old list cannot hold the new names, so stored
replies of those tools keep their (redacted) text and lose only the tool name.

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "ck_assistant_messages_tool_name_valid"
_OLD = (
    "'get_account_summary', 'get_recent_transactions', 'get_spend', 'get_balance_forecast', "
    "'get_loan_affordability', 'get_unusual_expenses'"
)
_ADDED = ("get_faq_answer", "list_products", "get_investment_suggestions")
_NEW = _OLD + ", " + ", ".join(f"'{name}'" for name in _ADDED)


def _replace(tools: str) -> None:
    op.drop_constraint(op.f(_NAME), "assistant_messages", type_="check")
    op.create_check_constraint(
        op.f(_NAME), "assistant_messages", f"tool_name IS NULL OR tool_name IN ({tools})"
    )


def upgrade() -> None:
    _replace(_NEW)


def downgrade() -> None:
    op.execute(
        "UPDATE assistant_messages SET tool_name = NULL WHERE tool_name IN "
        "('get_faq_answer', 'list_products', 'get_investment_suggestions')"
    )
    _replace(_OLD)
