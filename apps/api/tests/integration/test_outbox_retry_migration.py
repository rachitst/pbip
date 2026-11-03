"""TASK-026 / ADR-0040: the outbox retry migration keeps pending events in order, and its downgrade
returns dead letters to the queue instead of losing them.

Runs down to the previous revision and back up on the shared test database, so it leaves the
schema at head (other tests rely on it) even when an assertion fails.
"""

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, text

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
WRITTEN = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def alembic_config(migrated_engine: Engine, test_database_url: str) -> Iterator[Config]:
    os.environ["ALEMBIC_DATABASE_URL"] = test_database_url
    config = Config(str(ALEMBIC_INI))
    yield config
    command.upgrade(config, "head")


def _previous_revision(config: Config) -> str:
    """The revision before this migration (numbers are assigned at merge, so look it up)."""
    script = ScriptDirectory.from_config(config)
    [ours] = [r for r in script.walk_revisions() if r.path.endswith("_outbox_retry_dead_letter.py")]
    assert isinstance(ours.down_revision, str)
    return ours.down_revision


def test_upgrade_makes_pending_events_due_at_creation_and_downgrade_requeues_dead_letters(
    migrated_engine: Engine, alembic_config: Config
) -> None:
    dead = uuid.uuid4()
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO outbox_events (id, event_type, payload, created_at, attempts, "
                "next_attempt_at, dead_lettered_at) VALUES (:id, 'X', '{}', :at, 6, :at, :at)"
            ),
            {"id": dead, "at": WRITTEN},
        )
    command.downgrade(alembic_config, _previous_revision(alembic_config))

    pending = uuid.uuid4()
    with migrated_engine.begin() as conn:
        # The dead letter is plain unprocessed again: the old relay would retry it.
        assert conn.execute(
            text("SELECT processed_at IS NULL FROM outbox_events WHERE id = :id"), {"id": dead}
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO outbox_events (id, event_type, payload, created_at) "
                "VALUES (:id, 'X', '{}', :at)"
            ),
            {"id": pending, "at": WRITTEN},
        )

    command.upgrade(alembic_config, "head")
    with migrated_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, attempts, next_attempt_at, dead_lettered_at FROM outbox_events "
                "ORDER BY id"
            )
        ).all()
    assert sorted(
        (r.id, r.attempts, r.next_attempt_at, r.dead_lettered_at) for r in rows
    ) == sorted([(dead, 0, WRITTEN, None), (pending, 0, WRITTEN, None)])
