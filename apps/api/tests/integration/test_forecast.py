"""TASK-042: GET /api/v1/analytics/forecast (Predicted, not exact) against PostgreSQL."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pbip_api.modules.analytics import repository
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
    assert body["label"] == "Predicted, not exact" and body["status"] == "OK"
    assert body["method"] == "naive" and body["history_days"] >= 19
    assert len(body["points"]) == 30
    for p in body["points"]:
        assert p["lower_95_minor"] <= p["lower_80_minor"] <= p["predicted_minor"]
        assert p["predicted_minor"] <= p["upper_80_minor"] <= p["upper_95_minor"]
    assert body["points"][0]["predicted_minor"] == 50_000_00  # flat history, flat forecast


def test_forecast_needs_history_and_an_owned_account(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice, bob = make_customer(funds=10_000_00), make_customer(funds=10_000_00)
    # #382: a new account is an expected state, not an error: 200 with no figures.
    new = _forecast(client, alice, alice.account_id)
    assert new.status_code == 200, new.text
    body = new.json()
    assert body["status"] == "INSUFFICIENT_HISTORY" and body["method"] == "none"
    assert (body["points"], body["history"], body["obligations"]) == ([], [], [])
    assert body["end_of_month"] is None and body["history_days"] == 0
    assert body["account_id"] == str(alice.account_id) and body["horizon_days"] == 30
    assert _forecast(client, alice, bob.account_id).status_code == 404
    assert _forecast(client, alice, alice.account_id, 6).status_code == 422
    assert _forecast(client, alice, alice.account_id, 91).status_code == 422


def test_the_first_point_starts_from_the_balance_the_account_has_now(
    client: TestClient,
    make_customer: Callable[..., Actor],
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (TASK-089 bug bash, #384): with 14-27 days of history the weekly seasonal-naive
    model repeated last week's LEVELS, so after a step down six days ago the first point was the
    balance of a week ago and every later week swung back to it (Alice: INR 2,50,000 in 30 days
    against a ledger balance of INR 2,00,072). The ledger cannot be backdated (entries are
    immutable, stamped by the database), so the history is stubbed; the ledger balance is real."""
    alice = make_customer(funds=2_00_072_00)
    clock.advance(25 * 24 * 3600)
    stepped = [2_50_000_00] * 19 + [2_00_072_00] * 6  # 25 days, the step six days ago
    monkeypatch.setattr(repository, "daily_balances", lambda *_args, **_kw: stepped)

    resp = _forecast(client, alice, alice.account_id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["method"] == "naive"
    # No income and no obligations: the one-off debit does not come back on any day (#384).
    assert {p["predicted_minor"] for p in body["points"]} == {2_00_072_00}
    first = body["points"][0]
    assert first["predicted_minor"] == 2_00_072_00
    assert first["lower_95_minor"] <= first["lower_80_minor"] <= first["predicted_minor"]
    assert first["predicted_minor"] <= first["upper_80_minor"] <= first["upper_95_minor"]
