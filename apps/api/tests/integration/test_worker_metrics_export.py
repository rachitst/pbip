"""Issue #378 / ADR-0039: what the scheduled-transfer job counts in the worker reaches the API's
/metrics (the only application target Prometheus scrapes), against real PostgreSQL.

The test process stands in for the worker: it runs the job, then publishes like ``task_postrun``
does. Only ``source="worker"`` series are asserted; the same process's in-process series are an
artefact of running both roles in one test process.
"""

import uuid
from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
from fakeredis import FakeRedis
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.core.worker_metrics import REDIS_KEY, WorkerMetricsPublisher
from pbip_api.modules.payments import scheduled_service
from pbip_api.modules.payments.models import ScheduledTransfer, ScheduleFrequency
from pbip_api.modules.payments.scheduled_service import (
    ScheduledExecutionOutcome,
    process_due_scheduled_transfers,
)
from pbip_test_support import Actor, age_payee
from prometheus_client.parser import text_string_to_metric_families
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import Engine
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

TODAY = date(2026, 11, 1)


class DownRedis(FakeRedis):
    def pipeline(self, transaction: bool = True, shard_hint: object = None) -> object:
        raise RedisConnectionError("redis down")


@pytest.fixture(autouse=True)
def _worker_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(scheduled_service, "get_settings", lambda: settings)


@pytest.fixture
def due_instruction(
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> None:
    alice, bob = make_customer(funds=100_000), make_customer(funds=0)
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)  # baseline rules stay quiet: the transfer posts
    with Session(migrated_engine) as session:
        session.add(
            ScheduledTransfer(
                customer_id=alice.customer_id, user_id=alice.user_id,
                from_account_id=alice.account_id, payee_id=uuid.UUID(str(payee["id"])),
                to_account_id=None, amount=10_000, currency="INR", reference="Rent",
                frequency=ScheduleFrequency.MONTHLY, start_date=TODAY, next_run_date=TODAY,
            )
        )  # fmt: skip
        session.commit()


def worker_series(client: TestClient) -> dict[str, float]:
    body = client.get("/metrics")
    assert body.status_code == 200
    out: dict[str, float] = {}
    for family in text_string_to_metric_families(body.text):
        for s in family.samples:
            if s.labels.get("source") == "worker" and "le" not in s.labels:
                label = ",".join(f"{k}={v}" for k, v in sorted(s.labels.items()) if k != "source")
                out[f"{s.name}{{{label}}}"] = s.value
    return out


def test_scheduled_job_metrics_reach_the_api_metrics_endpoint(
    due_instruction: None, migrated_engine: Engine, fake_redis: FakeRedis, client: TestClient
) -> None:
    publisher = WorkerMetricsPublisher()
    publisher.flush(fake_redis)  # earlier tests' in-process increments are not this job's
    fake_redis.delete(REDIS_KEY)

    results = process_due_scheduled_transfers(migrated_engine, today_utc=TODAY)
    assert results[ScheduledExecutionOutcome.POSTED.value] == 1
    publisher.flush(fake_redis)

    series = worker_series(client)
    assert series["scheduled_transfers_total{outcome=POSTED}"] == 1
    assert series["scheduled_worker_duration_seconds_count{}"] == 1
    # The shared payment path the job runs is exported too, not only the worker-only metrics.
    assert series["transfers_total{status=POSTED}"] == 1
    assert series["fraud_decisions_total{decision=ALLOW}"] == 1


def test_the_job_succeeds_and_metrics_still_scrape_when_redis_is_down(
    due_instruction: None, migrated_engine: Engine, fake_redis: FakeRedis, client: TestClient
) -> None:
    publisher = WorkerMetricsPublisher()
    publisher.flush(fake_redis)
    fake_redis.delete(REDIS_KEY)

    results = process_due_scheduled_transfers(migrated_engine, today_utc=TODAY)
    assert results[ScheduledExecutionOutcome.POSTED.value] == 1
    assert publisher.flush(DownRedis(decode_responses=True)) == 0
    assert worker_series(client) == {}  # nothing published, scrape still 200

    assert publisher.flush(fake_redis) > 0  # Redis back: the pending delta is delivered
    assert worker_series(client)["scheduled_transfers_total{outcome=POSTED}"] == 1
