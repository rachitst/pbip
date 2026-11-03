"""TASK-068 follow-up #324 / ADR-0038: the hourly PSI job's maths, its Redis hand-off and the gauge.

The database half is in ``integration/test_feature_drift_job.py``; here the history is built in
memory so each edge case (too little data, stale or corrupted results, Redis down) is exact.
"""

import json
import random
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest
from fakeredis import FakeRedis
from pbip_api.modules.fraud import drift
from pbip_api.modules.ledger.service import PaymentObservation
from pbip_ml.monitoring.psi import PsiStatus, drift_report
from prometheus_client import REGISTRY
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.orm import Session

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 3, 6, 20, tzinfo=UTC)
ACCOUNTS = [UUID(int=i + 1) for i in range(8)]


def history(reference: int, current: int, current_scale: int = 1) -> list[PaymentObservation]:
    """``reference`` payments in [NOW-60d, NOW-30d) and ``current`` in [NOW-30d, NOW), seeded."""
    rng = random.Random(68)
    rows = []
    for count, start, scale in ((reference, NOW - 2 * drift.WINDOW, 1),
                                (current, NOW - drift.WINDOW, current_scale)):  # fmt: skip
        for i in range(count):
            at = start + timedelta(minutes=(i * 43_200) // max(count, 1))  # spread over 30 days
            amount = rng.randint(1_000, 500_000) * scale
            rows.append(PaymentObservation(amount, rng.choice(ACCOUNTS), at))
    return rows


def gauge(feature: str) -> float | None:
    return REGISTRY.get_sample_value("pbip_feature_psi", {"feature": feature})


@pytest.fixture(autouse=True)
def empty_gauge() -> None:
    drift.FEATURE_PSI.clear()


def test_drift_is_computed_with_window_end_now_and_matches_drift_report() -> None:
    rows = history(150, 150, current_scale=10)

    results = drift.compute_feature_drift(rows, NOW)

    expected = drift_report(
        [r.amount for r in rows], [r.debit_account_id for r in rows],
        [r.created_at for r in rows], window_end=NOW,
    )  # fmt: skip
    assert results == expected
    amount = next(r for r in results if r.feature == "amount")
    assert amount.status is PsiStatus.CRITICAL  # every current amount is ten times larger
    assert (amount.reference_size, amount.current_size) == (150, 150)


def test_payments_outside_the_two_windows_are_ignored() -> None:
    rows = history(150, 150)
    outside = [
        PaymentObservation(10**9, ACCOUNTS[0], NOW),  # the window end is exclusive
        PaymentObservation(10**9, ACCOUNTS[0], NOW - 2 * drift.WINDOW - timedelta(seconds=1)),
    ]
    assert drift.compute_feature_drift(rows + outside, NOW) == drift.compute_feature_drift(
        rows, NOW
    )


@pytest.mark.parametrize(("reference", "current"), [(0, 0), (99, 500), (500, 99), (0, 500)])
def test_too_little_data_in_either_window_gives_no_result_not_zero(
    reference: int, current: int
) -> None:
    assert drift.compute_feature_drift(history(reference, current), NOW) == []


def test_the_minimum_is_inclusive() -> None:
    assert len(drift.compute_feature_drift(history(100, 100), NOW)) == 2


def _store(redis: FakeRedis, results: list[dict[str, object]]) -> None:
    redis.set(drift.RESULT_KEY, json.dumps({"window_end": NOW.isoformat(), "results": results}))


def test_stored_results_become_one_gauge_series_per_feature() -> None:
    redis = FakeRedis(decode_responses=True)
    _store(redis, [
        {"feature": "amount", "psi": 0.31, "reference_size": 150, "current_size": 160},
        {"feature": "velocity", "psi": 0.04, "reference_size": 90, "current_size": 95},
    ])  # fmt: skip

    drift.publish_feature_psi(redis)

    assert gauge("amount") == 0.31
    assert gauge("velocity") == 0.04


def test_no_stored_result_means_no_series_even_after_an_earlier_value() -> None:
    redis = FakeRedis(decode_responses=True)
    _store(redis, [{"feature": "amount", "psi": 0.2, "reference_size": 1, "current_size": 1}])
    drift.publish_feature_psi(redis)
    assert gauge("amount") == 0.2

    redis.delete(drift.RESULT_KEY)  # expired (job stopped) or deleted (too little data)
    drift.publish_feature_psi(redis)

    assert gauge("amount") is None


@pytest.mark.parametrize(
    "item",
    [
        {"feature": "customer_id", "psi": 0.2, "reference_size": 1, "current_size": 1},
        {"feature": "amount", "psi": float("nan"), "reference_size": 1, "current_size": 1},
        {"feature": "amount", "psi": float("inf"), "reference_size": 1, "current_size": 1},
        {"feature": "amount", "psi": -0.5, "reference_size": 1, "current_size": 1},
        {"feature": "amount", "psi": "high", "reference_size": 1, "current_size": 1},
        {"feature": "amount"},
    ],
)
def test_a_corrupted_result_publishes_nothing(item: dict[str, object]) -> None:
    redis = FakeRedis(decode_responses=True)
    _store(redis, [{"feature": "velocity", "psi": 0.05, "reference_size": 1, "current_size": 1},
                   item])  # fmt: skip

    drift.publish_feature_psi(redis)

    assert gauge("velocity") is None
    assert gauge("amount") is None
    assert gauge("customer_id") is None


def test_unparseable_json_publishes_nothing() -> None:
    redis = FakeRedis(decode_responses=True)
    redis.set(drift.RESULT_KEY, "{not json")
    drift.publish_feature_psi(redis)
    assert gauge("amount") is None


class _DownRedis(FakeRedis):
    def get(self, name: object) -> None:  # type: ignore[override]  # a fake that always fails
        raise RedisConnectionError("down")


def test_redis_down_at_scrape_time_publishes_nothing_and_does_not_raise() -> None:
    drift.FEATURE_PSI.labels(feature="amount").set(0.3)
    drift.publish_feature_psi(_DownRedis(decode_responses=True))
    assert gauge("amount") is None


# The database is not touched: ``scored_payments_between`` is replaced in the test below.
NO_SESSION = cast(Session, object())


def test_the_job_stores_results_with_a_ttl_and_deletes_them_when_data_runs_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[datetime, datetime]] = []
    rows = history(150, 150, current_scale=10)

    def fake_read(_session: object, start: datetime, end: datetime) -> list[PaymentObservation]:
        seen.append((start, end))
        return rows

    monkeypatch.setattr(drift.ledger, "scored_payments_between", fake_read)
    redis = FakeRedis(decode_responses=True)

    results = drift.evaluate_feature_drift(NO_SESSION, redis, NOW)

    assert seen == [(NOW - timedelta(days=60), NOW)]
    stored = json.loads(str(redis.get(drift.RESULT_KEY)))
    assert stored["window_end"] == NOW.isoformat()
    assert {r["feature"]: r["psi"] for r in stored["results"]} == {
        r.feature: r.psi for r in results
    }
    ttl = int(redis.ttl(drift.RESULT_KEY))  # type: ignore[arg-type]  # sync client returns int
    assert 0 < ttl <= 3 * 3600
    assert not any(str(account) in json.dumps(stored) for account in ACCOUNTS)  # names only

    rows = history(150, 10)
    assert drift.evaluate_feature_drift(NO_SESSION, redis, NOW) == []
    assert redis.get(drift.RESULT_KEY) is None


def test_metrics_is_still_served_when_redis_is_down() -> None:
    """The scrape must not fail because the PSI store is unreachable: other metrics stay up."""
    from fastapi.testclient import TestClient
    from pbip_test_support import build_app

    drift.FEATURE_PSI.labels(feature="amount").set(0.3)
    with TestClient(build_app(redis_ok=False)) as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    assert "http_request_duration_seconds" in response.text
    assert 'pbip_feature_psi{feature="amount"}' not in response.text
