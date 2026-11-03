"""Issue #175: the models and the migrated schema agree, server defaults included.

`alembic check` in CI used to compare types but not server defaults, so a model default that the
migrations never created (or the reverse) went unnoticed. This runs the same autogenerate
comparison with ``compare_server_default`` on the real migrated database.
"""

import pbip_api.modules  # noqa: F401  (registers every model on Base.metadata)
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from pbip_api.core.db import Base
from sqlalchemy import Engine

pytestmark = pytest.mark.integration


def _not_a_partition(
    _object: object, name: str | None, _type: str, reflected: bool, _compare_to: object
) -> bool:
    # Same filter as alembic/env.py: monthly clickstream partitions are created at runtime.
    return not (reflected and name and name.startswith("clickstream_events_"))


def test_models_match_the_migrations_including_server_defaults(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        context = MigrationContext.configure(
            conn,
            opts={
                "compare_type": True,
                "compare_server_default": True,
                "include_object": _not_a_partition,
            },
        )
        diff = compare_metadata(context, Base.metadata)
    assert diff == []
