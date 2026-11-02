"""TASK-079: the SQLAlchemy pool is sized from validated settings, not hard-coded."""

import pytest
from pbip_api.core import db
from pbip_api.core.config import Settings
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def test_engine_pool_follows_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(db_pool_size=7, db_max_overflow=3, db_pool_timeout_seconds=4.5)
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    db.get_engine.cache_clear()
    try:
        pool = db.get_engine().pool
        assert pool.size() == 7  # type: ignore[attr-defined]  # QueuePool API, not on the Pool base
        assert pool._max_overflow == 3  # type: ignore[attr-defined]  # no public accessor
        assert pool._timeout == 4.5  # type: ignore[attr-defined]  # no public accessor
    finally:
        db.get_engine.cache_clear()


def test_defaults_are_unchanged_and_bounds_are_enforced() -> None:
    defaults = Settings()
    assert (defaults.db_pool_size, defaults.db_max_overflow, defaults.db_pool_timeout_seconds) == (
        10,
        10,
        30.0,
    )
    with pytest.raises(ValidationError):
        Settings(db_pool_size=0)
    with pytest.raises(ValidationError):
        Settings(db_max_overflow=-1)
    with pytest.raises(ValidationError):
        Settings(db_pool_timeout_seconds=0)
