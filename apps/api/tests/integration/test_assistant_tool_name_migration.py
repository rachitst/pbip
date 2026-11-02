"""TASK-105: the discovery-tools migration widens the stored tool-name CHECK, and its downgrade is safe.

Runs down to the previous revision and back up on the shared test database, so it leaves the
schema at head (other tests rely on it) even when an assertion fails.
"""

import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from pbip_test_support import Actor
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
NEW_TOOLS = ("get_faq_answer", "list_products", "get_investment_suggestions")


@pytest.fixture
def alembic_config(migrated_engine: Engine, test_database_url: str) -> Iterator[Config]:
    os.environ["ALEMBIC_DATABASE_URL"] = test_database_url
    config = Config(str(ALEMBIC_INI))
    yield config
    command.upgrade(config, "head")


def _previous_revision(config: Config) -> str:
    """The revision before this migration (numbers are assigned at merge, so look it up)."""
    script = ScriptDirectory.from_config(config)
    [ours] = [
        r for r in script.walk_revisions() if r.path.endswith("_assistant_discovery_tools.py")
    ]
    assert isinstance(ours.down_revision, str)
    return ours.down_revision


def _insert(engine: Engine, customer_id: uuid.UUID, tool: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO assistant_messages (id, customer_id, role, content, tool_name) "
                "VALUES (:id, :customer, 'assistant', 'stored reply', :tool)"
            ),
            {"id": uuid.uuid4(), "customer": customer_id, "tool": tool},
        )


def _stored(engine: Engine, customer_id: uuid.UUID) -> list[tuple[str, str | None]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT content, tool_name FROM assistant_messages WHERE customer_id = :c "
                "ORDER BY tool_name NULLS FIRST"
            ),
            {"c": customer_id},
        )
        return [(r.content, r.tool_name) for r in rows]


def test_tool_name_check_accepts_the_new_tools_and_downgrade_keeps_the_text(
    client: TestClient,
    make_customer: Callable[..., Actor],
    migrated_engine: Engine,
    alembic_config: Config,
) -> None:
    alice = make_customer()
    for tool in (*NEW_TOOLS, "get_unusual_expenses"):
        _insert(migrated_engine, alice.customer_id, tool)
    with pytest.raises(IntegrityError):
        _insert(migrated_engine, alice.customer_id, "transfer_money")

    command.downgrade(alembic_config, _previous_revision(alembic_config))
    # The new names are cleared, the (redacted) reply text stays; older names are untouched.
    assert _stored(migrated_engine, alice.customer_id) == [
        ("stored reply", None),
        ("stored reply", None),
        ("stored reply", None),
        ("stored reply", "get_unusual_expenses"),
    ]
    with pytest.raises(IntegrityError):
        _insert(migrated_engine, alice.customer_id, "list_products")

    command.upgrade(alembic_config, "head")
    _insert(migrated_engine, alice.customer_id, "list_products")
    assert ("stored reply", "list_products") in _stored(migrated_engine, alice.customer_id)
