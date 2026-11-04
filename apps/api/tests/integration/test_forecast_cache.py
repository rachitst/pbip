"""The forecast model result is reused only for exactly the same inputs (#175, ADR-0053).

The fit (AutoARIMA, about 1 s of CPU) is a pure function of the account's balance history up to
yesterday, the first forecast day and the horizon. Everything else in the response (today's
balance, scheduled obligations, the alert) is still read on every request.
"""

from collections.abc import Callable, Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pbip_api.modules.analytics import repository
from pbip_api.modules.analytics import service as analytics_service
from pbip_api.modules.analytics.projection import ModelDay
from pbip_api.modules.ledger import service as ledger
from pbip_test_support import Actor, FakeClock
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

pytestmark = pytest.mark.integration

DAY = 24 * 3600


@pytest.fixture(autouse=True)
def cold_cache() -> Iterator[None]:
    analytics_service.clear_forecast_cache()
    yield
    analytics_service.clear_forecast_cache()


@pytest.fixture
def fits(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The history length of every real model fit."""
    seen: list[int] = []
    real = analytics_service._model_days

    def spy(history: list[int], horizon: int, first_day: date) -> tuple[str, list[ModelDay]]:
        seen.append(len(history))
        return real(history, horizon, first_day)

    monkeypatch.setattr(analytics_service, "_model_days", spy)
    return seen


def _forecast(client: TestClient, actor: Actor) -> Response:
    resp = client.get(
        f"/api/v1/analytics/forecast?account_id={actor.account_id}&horizon_days=30",
        headers=actor.headers,
    )
    assert resp.status_code == 200, resp.text
    return resp


def test_the_same_history_is_fitted_once_and_today_stays_live(
    client: TestClient,
    make_customer: Callable[..., Actor],
    clock: FakeClock,
    fits: list[int],
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alice = make_customer(funds=50_000_00)
    assert alice.account_id is not None
    clock.advance(20 * DAY)
    first = _forecast(client, alice).json()
    assert _forecast(client, alice).json() == first
    assert len(fits) == 1
    analytics_service.clear_forecast_cache()
    assert _forecast(client, alice).json() == first  # a stored fit gives what a new fit gives
    assert len(fits) == 2

    # The ledger stamps entries with the database clock, so a deposit made "today" on the test
    # clock would land in the history; the history is pinned instead and the balance is real.
    history = [50_000_00] * 20
    monkeypatch.setattr(repository, "daily_balances", lambda *_args, **_kw: history)
    _forecast(client, alice)
    with sessionmaker(bind=migrated_engine)() as session:
        ledger.deposit(session, alice.account_id, 5_000_00, reference="Gift")
        session.commit()
    after = _forecast(client, alice).json()
    assert len(fits) == 2  # the same inputs, so no new fit ...
    assert after["points"][0]["predicted_minor"] == 55_000_00  # ... but today's balance is live

    clock.advance(DAY)  # a new first forecast day: the model runs again
    _forecast(client, alice)
    assert len(fits) == 3


def test_a_different_history_or_another_account_is_never_served_a_stored_fit(
    client: TestClient,
    make_customer: Callable[..., Actor],
    clock: FakeClock,
    fits: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alice, bob = make_customer(funds=1_000_00), make_customer(funds=1_000_00)
    clock.advance(25 * DAY)
    history = [2_50_000_00] * 19 + [2_00_072_00] * 6
    monkeypatch.setattr(repository, "daily_balances", lambda *_args, **_kw: history)
    _forecast(client, alice)
    _forecast(client, bob)  # identical inputs, but another customer's account: its own fit
    assert len(fits) == 2
    history[-1] = 2_00_000_00  # one day of history differs
    _forecast(client, alice)
    assert len(fits) == 3
