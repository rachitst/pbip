"""Shared fixtures. Unit tests use fakes; integration tests use the real PostgreSQL/Redis."""

import os
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_test_support import build_app
from sqlalchemy import create_engine, text


@pytest.fixture(autouse=True)
def _cold_read_caches() -> None:
    """Every test starts with ADR-0054's in-process read caches empty.

    They live as long as the process, so without this one test's entries could serve another test's
    requests and change what it measures (statement counts, timings). Tests that exercise the caches
    warm them on purpose.
    """
    from pbip_api.modules.analytics import service as analytics_service
    from pbip_api.modules.recommendations import service as recommendations_service

    recommendations_service.clear_context_cache()
    analytics_service.clear_forecast_cache()


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
        "postgresql+psycopg://pbip:pbip_local_password@127.0.0.1:5432/pbip_test",
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
