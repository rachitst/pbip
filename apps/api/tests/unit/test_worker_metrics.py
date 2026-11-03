"""Issue #378 / ADR-0039: metrics updated in a Celery worker reach Prometheus through Redis and the
API's /metrics, exactly once, with fixed labels, and Redis being down never breaks a job or a scrape."""

import json
from collections.abc import Iterator

import pytest
from fakeredis import FakeRedis
from fastapi.testclient import TestClient
from pbip_api.core import metrics, worker_metrics
from pbip_api.core.redis import get_redis
from pbip_api.core.worker_metrics import (
    MAX_SERIES,
    REDIS_KEY,
    WorkerMetricsPublisher,
    exported_metrics,
)
from pbip_test_support import build_app
from prometheus_client import Counter, Histogram
from prometheus_client.metrics import MetricWrapperBase
from prometheus_client.parser import text_string_to_metric_families
from redis.exceptions import ConnectionError as RedisConnectionError

pytestmark = pytest.mark.unit

# Set by the API itself from a value the worker stored (ADR-0038), so never published as a delta.
API_SIDE_GAUGES = {"pbip_feature_psi"}


class DownRedis(FakeRedis):
    def execute_command(self, *_args: object, **_kwargs: object) -> object:
        raise RedisConnectionError("redis down")

    def pipeline(self, transaction: bool = True, shard_hint: object = None) -> object:
        raise RedisConnectionError("redis down")


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis(decode_responses=True)


@pytest.fixture
def client(redis: FakeRedis) -> Iterator[TestClient]:
    app = build_app()
    app.dependency_overrides[get_redis] = lambda: redis
    with TestClient(app) as test_client:
        yield test_client


def worker_samples(client: TestClient) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    response = client.get("/metrics")
    assert response.status_code == 200
    return {
        (s.name, tuple(sorted((k, v) for k, v in s.labels.items() if k != "source"))): s.value
        for family in text_string_to_metric_families(response.text)
        for s in family.samples
        if s.labels.get("source") == "worker"
    }


def published(redis: FakeRedis) -> WorkerMetricsPublisher:
    """A publisher that has already flushed whatever earlier tests left in the in-process metrics."""
    publisher = WorkerMetricsPublisher()
    publisher.flush(redis)
    redis.delete(REDIS_KEY)
    return publisher


def test_a_worker_only_counter_appears_in_the_api_metrics_after_a_flush(
    redis: FakeRedis, client: TestClient
) -> None:
    publisher = published(redis)
    metrics.SCHEDULED_TRANSFERS_TOTAL.labels(outcome="POSTED").inc(2)
    metrics.SCHEDULED_MISSED_OCCURRENCES_TOTAL.inc()
    assert publisher.flush(redis) == 2

    samples = worker_samples(client)
    assert samples[("scheduled_transfers_total", (("outcome", "POSTED"),))] == 2
    assert samples[("scheduled_missed_occurrences_total", ())] == 1


def test_flushes_send_deltas_so_nothing_is_counted_twice(
    redis: FakeRedis, client: TestClient
) -> None:
    publisher = published(redis)
    metrics.FD_MATURITY_PAYOUTS_TOTAL.labels(outcome="PAID").inc()
    publisher.flush(redis)
    assert publisher.flush(redis) == 0  # nothing new: nothing written
    metrics.FD_MATURITY_PAYOUTS_TOTAL.labels(outcome="PAID").inc(3)
    publisher.flush(redis)

    assert worker_samples(client)[("fd_maturity_payouts_total", (("outcome", "PAID"),))] == 4


def test_two_worker_processes_add_up(redis: FakeRedis, client: TestClient) -> None:
    """Prefork children each flush their own deltas into the same hash (HINCRBYFLOAT)."""
    first, second = published(redis), published(redis)
    metrics.TRANSFERS_TOTAL.labels("DECLINED").inc()
    first.flush(redis)
    second.flush(redis)  # stands in for a second process that also counted one

    assert worker_samples(client)[("transfers_total", (("status", "DECLINED"),))] == 2


def test_a_histogram_is_exported_with_every_bucket(redis: FakeRedis, client: TestClient) -> None:
    publisher = published(redis)
    metrics.SCHEDULED_WORKER_DURATION_SECONDS.observe(0.3)
    publisher.flush(redis)

    samples = worker_samples(client)
    buckets = {dict(k[1])["le"]: v for k, v in samples.items() if k[0].endswith("_bucket")}
    assert buckets["0.25"] == 0 and buckets["0.5"] == 1 and buckets["+Inf"] == 1
    assert len(buckets) == len(Histogram.DEFAULT_BUCKETS)
    assert samples[("scheduled_worker_duration_seconds_count", ())] == 1
    assert samples[("scheduled_worker_duration_seconds_sum", ())] == pytest.approx(0.3)


def test_redis_down_at_flush_keeps_the_delta_for_the_next_flush(
    redis: FakeRedis, client: TestClient
) -> None:
    publisher = published(redis)
    metrics.SCHEDULED_TRANSFERS_TOTAL.labels(outcome="BLOCK").inc()
    assert publisher.flush(DownRedis(decode_responses=True)) == 0  # no exception
    assert publisher.flush(redis) == 1

    assert worker_samples(client)[("scheduled_transfers_total", (("outcome", "BLOCK"),))] == 1


def test_redis_down_at_scrape_still_serves_metrics_without_worker_series() -> None:
    app = build_app(redis_ok=False)
    with TestClient(app) as test_client:
        response = test_client.get("/metrics")
    assert response.status_code == 200
    assert "http_requests_total" in response.text
    assert 'source="worker"' not in response.text


def test_corrupted_or_foreign_fields_never_become_series(
    redis: FakeRedis, client: TestClient
) -> None:
    good = json.dumps(["scheduled_transfers_total", [["outcome", "POSTED"]]])
    redis.hset(
        REDIS_KEY,
        mapping={
            good: "1",
            json.dumps(["unknown_total", []]): "1",  # not an application metric
            json.dumps(["scheduled_transfers_total", [["account_id", "x"]]]): "1",  # wrong label
            json.dumps(["scheduled_transfers_total", [["outcome", 'x"} 1\nevil']]]): "1",
            json.dumps(["scheduled_transfers_total", [["outcome", "REVIEW"]]]): "-5",  # negative
            json.dumps(["scheduled_transfers_total", [["outcome", "BLOCK"]]]): "nan",
            "not json": "1",
            json.dumps([["scheduled_transfers_total"], []]): "1",  # unhashable name
            json.dumps({"a": 1, "b": 2}): "1",
        },
    )
    assert worker_samples(client) == {("scheduled_transfers_total", (("outcome", "POSTED"),)): 1}


def test_an_oversized_hash_is_ignored_entirely(redis: FakeRedis, client: TestClient) -> None:
    redis.hset(
        REDIS_KEY,
        mapping={
            json.dumps(["scheduled_transfers_total", [["outcome", f"V{i}"]]]): "1"
            for i in range(MAX_SERIES + 1)
        },
    )
    assert worker_samples(client) == {}


def test_only_a_marked_worker_process_publishes_after_a_task(
    monkeypatch: pytest.MonkeyPatch, redis: FakeRedis
) -> None:
    """The API process never publishes, so its in-process increments are not exported twice."""
    from pbip_api.worker.tasks import ping

    publisher = published(redis)
    monkeypatch.setattr(worker_metrics, "_PUBLISHER", publisher)
    monkeypatch.setattr(worker_metrics, "get_redis", lambda: redis)
    monkeypatch.setitem(worker_metrics._role, "worker", False)
    metrics.SCHEDULED_TRANSFERS_TOTAL.labels(outcome="REPLAY").inc()

    ping.apply()  # an eager run fires task_postrun in this (non-worker) process
    assert redis.hgetall(REDIS_KEY) == {}

    from celery.signals import worker_process_init

    worker_process_init.send(sender=None)
    assert worker_metrics.is_worker_process()
    assert ping.apply().get() == "pong"
    assert redis.hgetall(REDIS_KEY) == {
        json.dumps(
            ["scheduled_transfers_total", [["outcome", "REPLAY"]]], separators=(",", ":")
        ): "1"
    }


def test_a_task_succeeds_when_redis_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    from pbip_api.worker.tasks import ping

    monkeypatch.setattr(worker_metrics, "_PUBLISHER", WorkerMetricsPublisher())
    monkeypatch.setattr(worker_metrics, "get_redis", lambda: DownRedis(decode_responses=True))
    monkeypatch.setitem(worker_metrics._role, "worker", True)
    metrics.SCHEDULED_TRANSFERS_TOTAL.labels(outcome="SKIPPED_LOCKED").inc()

    result = ping.apply()
    assert result.successful() and result.get() == "pong"


# ------------------------------------------------------------------ guard: nothing stays in a worker
def test_every_application_metric_is_exported_from_the_worker_or_set_by_the_api() -> None:
    """A metric type the publisher cannot carry (a Gauge, a Summary...) set in a job would never
    reach Prometheus: it must be added to the publisher (ADR-0039) or be set by the API itself."""
    defined = {v._name: v for v in vars(metrics).values() if isinstance(v, MetricWrapperBase)}
    carried = {m._name for m in exported_metrics()}
    assert set(defined) - carried == API_SIDE_GAUGES
    assert all(isinstance(defined[name], Counter | Histogram) for name in carried)


def test_every_counter_and_histogram_reaches_the_api_metrics(
    redis: FakeRedis, client: TestClient
) -> None:
    """Each metric is updated as a worker would, published, then must appear with source=worker."""
    publisher = published(redis)
    for metric in exported_metrics():
        labels = dict.fromkeys(metric._labelnames, "GUARD_TEST")
        target = metric.labels(**labels) if labels else metric
        if isinstance(target, Histogram):
            target.observe(0.01)
        else:
            target.inc()
    publisher.flush(redis)

    exported = {name for name, _ in worker_samples(client)}
    for metric in exported_metrics():
        family = next(iter(metric.describe())).name
        suffix = "_count" if isinstance(metric, Histogram) else "_total"
        assert f"{family}{suffix}" in exported, family


def test_no_application_metric_uses_the_source_label() -> None:
    for metric in exported_metrics():
        assert "source" not in metric._labelnames, metric._name
