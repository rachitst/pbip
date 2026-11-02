"""Prometheus metrics. Metric names are a public contract used by the Grafana dashboards.

Label values must be low-cardinality and free of personal data: use route templates
(``/api/v1/accounts/{account_id}``), never raw paths, user ids or account numbers.
"""

from collections.abc import Iterable

from pbip_ml.monitoring.psi import PsiResult
from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total", "Total HTTP requests", ["method", "route", "status"]
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

FRAUD_INFERENCE_SECONDS = Histogram(
    "fraud_inference_seconds",
    "Fraud scoring latency in seconds",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
FRAUD_DECISIONS_TOTAL = Counter("fraud_decisions_total", "Fraud decisions by outcome", ["decision"])
TRANSFERS_TOTAL = Counter("transfers_total", "Transfers by final status", ["status"])
IDEMPOTENT_REPLAYS_TOTAL = Counter(
    "idempotent_replays_total", "Requests answered from a stored idempotent response"
)
LOGIN_FAILURES_TOTAL = Counter("login_failures_total", "Failed login attempts", ["reason"])
SCHEDULED_TRANSFERS_TOTAL = Counter(
    "scheduled_transfers_total", "Scheduled transfer executions by outcome", ["outcome"]
)
SCHEDULED_MISSED_OCCURRENCES_TOTAL = Counter(
    "scheduled_missed_occurrences_total", "Missed scheduled occurrences recorded"
)
SCHEDULED_WORKER_DURATION_SECONDS = Histogram(
    "scheduled_worker_duration_seconds",
    "Duration of scheduled transfer processing loop in seconds",
)
UNHANDLED_ERRORS_TOTAL = Counter(
    "unhandled_errors_total",
    "Unhandled exceptions answered with a generic 500 (error tracking, TASK-103)",
    ["route", "error_type"],
)
FEATURE_PSI = Gauge(
    "pbip_feature_psi",
    "Population Stability Index of a fraud-model input feature, current vs reference window "
    "(TASK-068; < 0.1 stable, > 0.1 warning, > 0.25 critical)",
    ["feature"],
)


def record_feature_psi(results: Iterable[PsiResult]) -> None:
    """Publish PSI results as ``pbip_feature_psi{feature=...}``; labels are feature names only."""
    for result in results:
        FEATURE_PSI.labels(feature=result.feature).set(result.psi)
