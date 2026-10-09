from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

pytestmark = pytest.mark.unit

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def test_there_is_exactly_one_migration_head() -> None:
    """Two heads mean two branches were merged without reconciling migrations."""
    heads = ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_heads()
    assert len(heads) == 1, f"Multiple Alembic heads: {heads}. Create a merge migration."
