"""TASK-042: GET /api/v1/analytics/forecast (Predicted, not exact) against PostgreSQL."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pbip_test_support import Actor, FakeClock

pytestmark = pytest.mark.integration


def _forecast(client: TestClient, actor: Actor, account: object, horizon: int = 30) -> Response:
    return client.get(
        f"/api/v1/analytics/forecast?account_id={account}&horizon_days={horizon}",
        headers=actor.headers,
    )


def test_forecast_is_labelled_with_intervals_from_the_account_history(
    client: TestClient, make_customer: Callable[..., Actor], clock: FakeClock
) -> None:
    alice = make_customer(funds=50_000_00)
    clock.advance(20 * 24 * 3600)  # twenty days of (constant) balance history behind "today"
    resp = _forecast(client, alice, alice.account_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["label"] == "Predicted, not exact"
    assert body["method"] == "seasonal_naive" and body["history_days"] >= 19
    assert len(body["points"]) == 30
    for p in body["points"]:
        assert p["lower_95_minor"] <= p["lower_80_minor"] <= p["predicted_minor"]
        assert p["predicted_minor"] <= p["upper_80_minor"] <= p["upper_95_minor"]
    assert body["points"][0]["predicted_minor"] == 50_000_00  # flat history, flat forecast


def test_forecast_needs_history_and_an_owned_account(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice, bob = make_customer(funds=10_000_00), make_customer(funds=10_000_00)
    assert _forecast(client, alice, alice.account_id).status_code == 422
    assert _forecast(client, alice, bob.account_id).status_code == 404
    assert _forecast(client, alice, alice.account_id, 6).status_code == 422
    assert _forecast(client, alice, alice.account_id, 91).status_code == 422
