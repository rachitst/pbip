"""The application engine bounds how long opening a PostgreSQL connection may take.

Regression for #131: with no connect timeout (libpq's default), a connection attempt that stalls
(Windows "localhost" -> ::1, a stopped or unreachable database) blocked a Celery task forever, so
the worker's warm shutdown (first Ctrl+C) never finished.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator

import pytest
from pbip_api.core import db
from pbip_api.core.config import Settings, get_settings
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

pytestmark = pytest.mark.unit


@pytest.fixture
def silent_server() -> Iterator[int]:
    """A local TCP port that accepts connections but never answers (a stalled server)."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    try:
        yield server.getsockname()[1]
    finally:
        server.close()


@pytest.fixture
def engine_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    get_settings.cache_clear()
    db.get_engine.cache_clear()
    yield
    get_settings.cache_clear()
    db.get_engine.cache_clear()


def test_engine_gives_up_on_a_stalled_connect(
    silent_server: int, engine_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"postgresql+psycopg://u:p@127.0.0.1:{silent_server}/db"  # pragma: allowlist secret
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATABASE_CONNECT_TIMEOUT_SECONDS", "2")
    engine = db.get_engine()
    started = time.monotonic()
    with pytest.raises(OperationalError), engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    assert time.monotonic() - started < 15
    engine.dispose()


def test_connect_timeout_setting_is_bounded() -> None:
    assert Settings().database_connect_timeout_seconds == 5
    for bad in (0, 1, 61):
        with pytest.raises(ValidationError):
            Settings(database_connect_timeout_seconds=bad)
