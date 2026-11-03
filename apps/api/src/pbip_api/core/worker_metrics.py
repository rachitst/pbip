"""Worker counters and histograms reach Prometheus through Redis and the API's /metrics (ADR-0039).

Prometheus scrapes only the API. A Celery task runs in a worker process (a prefork child), so what
it adds to a ``prometheus_client`` metric stays in that process's memory. After every task the
worker adds what changed since its last flush to one Redis hash (``HINCRBYFLOAT``, one MULTI/EXEC),
and the API's ``/metrics`` appends the stored totals to the same families with ``source="worker"``.

* No double counting: only a process marked by Celery's ``worker_init`` / ``worker_process_init``
  flushes, and it flushes deltas, so the API's own increments are counted once, in-process.
* Monotonic: the hash has no TTL and is only ever incremented. A Redis flush resets the totals to
  zero, which Prometheus treats as an ordinary counter reset (``rate``/``increase`` stay correct).
* Fail open: Redis down at flush time keeps the delta pending for the next task; Redis down or a
  corrupted hash at scrape time means no worker series, never a failed scrape or a failed job.
* Fixed cardinality: a stored series is exported only if its sample name and label names belong to
  a metric in ``core/metrics.py`` and its label values are short identifiers; the whole hash is
  ignored above ``MAX_SERIES`` fields.
"""

import json
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from threading import Lock
from typing import cast

import structlog
from prometheus_client import REGISTRY, Counter, Histogram
from prometheus_client.metrics_core import Metric
from prometheus_client.registry import Collector
from prometheus_client.samples import Sample
from redis import Redis
from redis.exceptions import RedisError

from pbip_api.core import metrics
from pbip_api.core.redis import get_redis

_logger = structlog.get_logger(__name__)

REDIS_KEY = "pbip:metrics:worker:v1"
SOURCE_LABEL = "source"
SOURCE_WORKER = "worker"
MAX_SERIES = 1000  # today's metrics need well under 200; above this the hash is not trusted
_LABEL_VALUE = re.compile(r"^[A-Za-z0-9_.+-]{1,64}$")  # enum values and bucket bounds only

Labels = tuple[tuple[str, str], ...]
SeriesKey = tuple[str, Labels]
ExportedMetric = Counter | Histogram


def exported_metrics() -> tuple[ExportedMetric, ...]:
    """Every counter and histogram the application defines; a worker can update any of them."""
    return tuple(v for v in vars(metrics).values() if isinstance(v, Counter | Histogram))


def _family(metric: ExportedMetric) -> str:
    return next(iter(metric.describe())).name


def _sample_label_names(metric: ExportedMetric) -> dict[str, frozenset[str]]:
    """Sample name -> its exact label names (only a histogram's buckets carry ``le``)."""
    # prometheus_client has no public accessor for label names (its version is pinned in uv.lock).
    names = frozenset(metric._labelnames)
    family = _family(metric)
    if isinstance(metric, Histogram):
        return {
            f"{family}_bucket": names | {"le"},
            f"{family}_count": names,
            f"{family}_sum": names,
        }
    return {f"{family}_total": names}


def _series(metric: ExportedMetric) -> dict[SeriesKey, float]:
    out: dict[SeriesKey, float] = {}
    for family in metric.collect():
        for sample in family.samples:
            if not sample.name.endswith("_created"):  # a per-process timestamp, not a count
                out[(sample.name, tuple(sorted(sample.labels.items())))] = sample.value
    return out


def _group(key: SeriesKey) -> tuple[str, Labels]:
    """A histogram's buckets, count and sum change together and must be written together."""
    return key[0].rsplit("_", 1)[0], tuple(pair for pair in key[1] if pair[0] != "le")


def _field(key: SeriesKey) -> str:
    return json.dumps([key[0], [list(pair) for pair in key[1]]], separators=(",", ":"))


class WorkerMetricsPublisher:
    """Pushes this process's metric increments to Redis as deltas since its last good flush."""

    def __init__(self) -> None:
        self._tracked = exported_metrics()
        self._flushed: dict[SeriesKey, float] = {}
        self._lock = Lock()

    def _deltas(self) -> tuple[dict[SeriesKey, float], dict[SeriesKey, float]]:
        deltas: dict[SeriesKey, float] = {}
        current: dict[SeriesKey, float] = {}
        for metric in self._tracked:
            series = _series(metric)
            changed = {_group(k) for k, v in series.items() if v != self._flushed.get(k, 0.0)}
            for key, value in series.items():
                if _group(key) in changed:
                    deltas[key] = value - self._flushed.get(key, 0.0)
                    current[key] = value
        return deltas, current

    def flush(self, redis_client: Redis) -> int:
        """Write the pending deltas atomically; returns how many series were written (0 on failure).

        Never raises for Redis: a failed flush leaves the deltas pending for the next one."""
        with self._lock:
            deltas, current = self._deltas()
            if not deltas:
                return 0
            try:
                pipe = redis_client.pipeline(transaction=True)
                for key, delta in deltas.items():
                    pipe.hincrbyfloat(REDIS_KEY, _field(key), delta)
                pipe.execute()
            except RedisError as exc:
                _logger.warning(
                    "worker_metrics_flush_failed",
                    error_type=type(exc).__name__,
                    pending_series=len(deltas),
                )
                return 0
            self._flushed.update(current)
            return len(deltas)


def _allowed() -> dict[str, tuple[str, frozenset[str]]]:
    """Sample name -> (family name, label names) for every exported metric."""
    allowed: dict[str, tuple[str, frozenset[str]]] = {}
    for metric in exported_metrics():
        for name, label_names in _sample_label_names(metric).items():
            allowed[name] = (_family(metric), label_names)
    return allowed


def _parse(
    field: str, raw_value: str, allowed: Mapping[str, tuple[str, frozenset[str]]]
) -> tuple[str, Sample] | None:
    try:
        name, pairs = json.loads(field)
        if not isinstance(name, str) or not isinstance(pairs, list):
            return None
        labels = {str(k): str(v) for k, v in pairs}
        value = float(raw_value)
    except (ValueError, TypeError):
        return None
    if name not in allowed or not math.isfinite(value) or value < 0:
        return None
    family, label_names = allowed[name]
    if set(labels) != label_names or not all(_LABEL_VALUE.match(v) for v in labels.values()):
        return None
    return family, Sample(name, {**labels, SOURCE_LABEL: SOURCE_WORKER}, value)


def _order(sample: Sample) -> tuple[Labels, str, float]:
    le = sample.labels.get("le")
    group = tuple(sorted((k, v) for k, v in sample.labels.items() if k != "le"))
    return group, sample.name, float(le) if le is not None else 0.0


def read_worker_samples(redis_client: Redis) -> dict[str, list[Sample]]:
    """The stored worker totals as samples per family name; empty when Redis cannot be trusted."""
    try:
        # The sync client with decode_responses=True (core/redis.py) returns dict[str, str].
        stored = cast(dict[str, str], redis_client.hgetall(REDIS_KEY))
    except RedisError as exc:
        _logger.warning("worker_metrics_unavailable", error_type=type(exc).__name__)
        return {}
    if len(stored) > MAX_SERIES:
        _logger.warning("worker_metrics_too_many_series", series=len(stored))
        return {}
    allowed = _allowed()
    families: dict[str, list[Sample]] = defaultdict(list)
    invalid = 0
    for field, raw_value in stored.items():
        parsed = _parse(field, raw_value, allowed)
        if parsed is None:
            invalid += 1
            continue
        families[parsed[0]].append(parsed[1])
    if invalid:
        _logger.warning("worker_metrics_invalid_series", series=invalid)
    return {family: sorted(samples, key=_order) for family, samples in families.items()}


class MergedRegistry(Collector):
    """The default registry with the worker's stored totals appended to their own families."""

    def __init__(self, worker_samples: Mapping[str, list[Sample]]) -> None:
        self._worker_samples = worker_samples

    def collect(self) -> Iterable[Metric]:
        for family in REGISTRY.collect():
            family.samples.extend(self._worker_samples.get(family.name, ()))
            yield family


_role = {"worker": False}
_PUBLISHER = WorkerMetricsPublisher()


def mark_worker_process(**_: object) -> None:
    """Celery ``worker_init`` / ``worker_process_init`` receiver: this process runs tasks."""
    _role["worker"] = True


def is_worker_process() -> bool:
    return _role["worker"]


def flush_after_task(**_: object) -> None:
    """Celery ``task_postrun`` receiver. Outside a worker (eager calls in the API, tests) it does
    nothing, so an API-side increment is never also published from Redis."""
    if is_worker_process():
        _PUBLISHER.flush(get_redis())
