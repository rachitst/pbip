"""Shared fixtures. Unit tests use fakes; integration tests use the real PostgreSQL/Redis."""

import os
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_test_support import build_app
from sqlalchemy import create_engine, text


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(build_app()) as test_client:
        yield test_client


def _skip_or_fail(reason: str) -> None:
    """Locally: skip with a hint. In CI: a missing service is a hard failure."""
    if os.environ.get("CI"):
        pytest.fail(reason)
    pytest.skip(f"{reason} - run `make infra-up` first")


@pytest.fixture(scope="session")
def test_database_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://pbip:pbip_local_password@localhost:5432/pbip_test",
    )


@pytest.fixture(scope="session")
def db_engine(test_database_url: str) -> Iterator[Any]:
    engine = create_engine(test_database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        _skip_or_fail("PostgreSQL test database is not reachable")
    yield engine
    engine.dispose()
