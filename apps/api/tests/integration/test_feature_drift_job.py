"""TASK-068 follow-up #324 / ADR-0038: the hourly PSI job against real PostgreSQL, end to end.

Payments are seeded as declined transfers (headers only, created through ``ledger.create_transaction``;
no entries, so no balance moves) at fixed instants. The job runs with a fixed ``now`` (the window end),
writes its result to Redis, and the API's ``/metrics`` (the process Prometheus scrapes) exports it.
"""

import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fakeredis import FakeRedis
from fastapi.testclient import TestClient
from pbip_api.modules.fraud import drift
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import TransactionStatus, TransactionType
from pbip_ml.monitoring.psi import WARNING_THRESHOLD, PsiStatus, drift_report
from pbip_test_support import Actor
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 3, 6, 20, tzinfo=UTC)
DAY = timedelta(days=1)


def seed(
    db: Session,
    accounts: list[Actor],
    count: int,
    start: datetime,
    scale: int,
    type_: TransactionType = TransactionType.TRANSFER,
) -> None:
    """``count`` scored payments spread over the 30 days from ``start``; amounts seeded."""
    rng = random.Random(f"{start.isoformat()}-{count}-{scale}")
    for i in range(count):
        sender = rng.choice(accounts)
        txn = ledger.create_transaction(
            db, type_=type_, amount=rng.randint(1_000, 500_000) * scale, currency="INR",
            debit_account_id=sender.account_id, credit_account_id=ledger.SYSTEM_CLEARING_ID,
        )  # fmt: skip
        txn.status = TransactionStatus.DECLINED
        txn.created_at = start + timedelta(minutes=(i * 43_200) // count)
    db.commit()


def gauge_series(client: TestClient) -> dict[str, float]:
    body = client.get("/metrics").text
    return {
        sample.labels["feature"]: sample.value
        for family in text_string_to_metric_families(body)
        if family.name == "pbip_feature_psi"
        for sample in family.samples
    }


@pytest.fixture
def senders(make_customer: Callable[..., Actor]) -> list[Actor]:
    return [make_customer() for _ in range(3)]


@pytest.fixture(autouse=True)
def empty_gauge() -> None:
    drift.FEATURE_PSI.clear()


def test_the_job_computes_psi_from_the_ledger_and_metrics_exports_it(
    db: Session, senders: list[Actor], fake_redis: FakeRedis, client: TestClient
) -> None:
    seed(db, senders, 150, NOW - 60 * DAY, scale=1)
    seed(db, senders, 150, NOW - 30 * DAY, scale=10)  # every current amount ten times larger

    results = drift.evaluate_feature_drift(db, fake_redis, NOW)

    rows = ledger.scored_payments_between(db, NOW - 60 * DAY, NOW)
    assert len(rows) == 300
    expected = drift_report(
        [r.amount for r in rows], [r.debit_account_id for r in rows],
        [r.created_at for r in rows], window_end=NOW,
    )  # fmt: skip
    assert results == expected
    by_feature = {r.feature: r for r in results}
    assert by_feature["amount"].status is PsiStatus.CRITICAL
    assert gauge_series(client) == pytest.approx({r.feature: r.psi for r in results})


def test_a_stable_population_stays_below_the_warning_threshold(
    db: Session, senders: list[Actor], fake_redis: FakeRedis, client: TestClient
) -> None:
    seed(db, senders, 300, NOW - 60 * DAY, scale=1)
    seed(db, senders, 300, NOW - 30 * DAY, scale=1)

    drift.evaluate_feature_drift(db, fake_redis, NOW)

    series = gauge_series(client)
    assert set(series) == {"amount", "velocity"}
    assert series["amount"] < WARNING_THRESHOLD


def test_bill_payments_count_and_deposits_and_other_windows_do_not(
    db: Session, senders: list[Actor]
) -> None:
    seed(db, senders, 5, NOW - 30 * DAY, scale=1, type_=TransactionType.BILL_PAYMENT)
    seed(db, senders, 5, NOW - 90 * DAY, scale=1)  # before the reference window
    seed(db, senders, 5, NOW, scale=1)  # at and after the window end (exclusive)
    ledger.deposit(db, senders[0].account_id, 10_000)  # not a scored outgoing payment
    db.commit()

    rows = ledger.scored_payments_between(db, NOW - 60 * DAY, NOW)

    assert len(rows) == 5
    assert all(NOW - 60 * DAY <= r.created_at < NOW for r in rows)


def test_too_little_history_exports_no_series_and_clears_an_earlier_one(
    db: Session, senders: list[Actor], fake_redis: FakeRedis, client: TestClient
) -> None:
    seed(db, senders, 150, NOW - 60 * DAY, scale=1)
    seed(db, senders, 150, NOW - 30 * DAY, scale=10)
    drift.evaluate_feature_drift(db, fake_redis, NOW)
    assert set(gauge_series(client)) == {"amount", "velocity"}

    # Thirty days later the old reference window has slid out: only 150 payments remain, all in
    # the new reference window, and the current window is empty.
    assert drift.evaluate_feature_drift(db, fake_redis, NOW + 30 * DAY) == []

    assert gauge_series(client) == {}
    assert fake_redis.get(drift.RESULT_KEY) is None
