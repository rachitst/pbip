"""Scheduled PSI drift evaluation of the fraud scorer's inputs (TASK-068 follow-up #324, ADR-0038).

Two halves, in two processes:

* **The worker** (hourly beat task) reads the last two 30-day windows of scored payments through
  ``ledger.service``, runs ``pbip_ml.monitoring.drift_report`` with ``window_end = now`` and stores
  the results in Redis under one key with a TTL.
* **The API** is the process Prometheus scrapes. Its ``/metrics`` handler calls
  ``publish_feature_psi``, which copies the stored results into the ``pbip_feature_psi{feature}``
  gauge (one Redis GET per scrape; no SQL and no PSI maths on the scrape path).

No data is not zero. When either window has fewer than ``MIN_WINDOW_PAYMENTS`` payments, or the
job has not run within ``RESULT_TTL`` (two missed runs), the key is gone and the gauge has no
series at all, so neither drift alert can fire on a value nobody computed. Zero would claim
"stable", which would be invented data.

Only feature names (``amount``, ``velocity``) and numbers are stored and exported: no account
ids, amounts or customer data leave the worker.
"""

import json
import math
from datetime import datetime, timedelta

import structlog
from pbip_ml.monitoring.psi import (
    DEFAULT_WINDOW_DAYS,
    FEATURE_AMOUNT,
    FEATURE_VELOCITY,
    PsiResult,
    classify,
    drift_report,
)
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from pbip_api.core.metrics import FEATURE_PSI, record_feature_psi
from pbip_api.modules.ledger import service as ledger

_logger = structlog.get_logger(__name__)

RESULT_KEY = "fraud:psi:v1"
# The job runs hourly; a result older than two missed runs is stale and must stop alerting.
RESULT_TTL = timedelta(hours=3)
WINDOW = timedelta(days=DEFAULT_WINDOW_DAYS)
# Ten decile bins need enough mass per bin for PSI to mean anything: below 10 payments per bin
# on average (100 per window) the index measures sampling noise, not drift.
MIN_WINDOW_PAYMENTS = 100
_KNOWN_FEATURES = frozenset({FEATURE_AMOUNT, FEATURE_VELOCITY})


def compute_feature_drift(
    observations: list[ledger.PaymentObservation], now: datetime
) -> list[PsiResult]:
    """PSI of amount and velocity, current ``[now-30d, now)`` vs reference ``[now-60d, now-30d)``.

    Returns ``[]`` (no result, not a zero) when either window holds fewer than
    ``MIN_WINDOW_PAYMENTS`` payments.
    """
    split = now - WINDOW
    current = sum(1 for o in observations if split <= o.created_at < now)
    reference = sum(1 for o in observations if now - 2 * WINDOW <= o.created_at < split)
    if min(current, reference) < MIN_WINDOW_PAYMENTS:
        _logger.info(
            "feature_psi_insufficient_data",
            reference_size=reference,
            current_size=current,
            minimum=MIN_WINDOW_PAYMENTS,
        )
        return []
    return drift_report(
        amounts=[o.amount for o in observations],
        entity_keys=[o.debit_account_id for o in observations],
        timestamps=[o.created_at for o in observations],
        window_end=now,
        window_days=DEFAULT_WINDOW_DAYS,
    )


def evaluate_feature_drift(session: Session, redis_client: Redis, now: datetime) -> list[PsiResult]:
    """The beat job's use case: read the history, compute PSI and store it for ``/metrics``.

    Read-only on PostgreSQL. Idempotent: a re-delivered run overwrites the key with the same value.
    A Redis failure propagates, so the task is recorded as failed (``CeleryTaskFailures``).
    """
    observations = ledger.scored_payments_between(session, now - 2 * WINDOW, now)
    results = compute_feature_drift(observations, now)
    if not results:
        redis_client.delete(RESULT_KEY)
        return []
    payload = {
        "window_end": now.isoformat(),
        "results": [
            {
                "feature": r.feature,
                "psi": r.psi,
                "reference_size": r.reference_size,
                "current_size": r.current_size,
            }
            for r in results
        ],
    }
    redis_client.set(RESULT_KEY, json.dumps(payload), ex=int(RESULT_TTL.total_seconds()))
    for r in results:
        _logger.info("feature_psi_evaluated", feature=r.feature, psi=r.psi, status=r.status.value)
    return results


def _parse(raw: str) -> list[PsiResult]:
    """Stored results, re-validated: only known feature names and finite non-negative numbers
    become gauge series, so a corrupted key cannot add labels or publish garbage."""
    results: list[PsiResult] = []
    for item in json.loads(raw)["results"]:
        feature, psi = item["feature"], float(item["psi"])
        if feature not in _KNOWN_FEATURES or not math.isfinite(psi) or psi < 0:
            raise ValueError("invalid stored PSI result")
        results.append(
            PsiResult(
                feature, psi, classify(psi), int(item["reference_size"]), int(item["current_size"])
            )
        )
    return results


def publish_feature_psi(redis_client: Redis) -> None:
    """Refresh ``pbip_feature_psi`` from the stored results (called by the API's ``/metrics``).

    The gauge always mirrors the key: no key, an unreadable key or Redis down all mean no series.
    """
    FEATURE_PSI.clear()
    try:
        raw = redis_client.get(RESULT_KEY)
    except RedisError as exc:
        _logger.warning("feature_psi_unavailable", error_type=type(exc).__name__)
        return
    if raw is None:
        return
    try:
        results = _parse(str(raw))
    except (ValueError, KeyError, TypeError) as exc:
        _logger.warning("feature_psi_invalid", error_type=type(exc).__name__)
        return
    record_feature_psi(results)
