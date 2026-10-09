"""Prometheus metrics. Metric names are a public contract used by the Grafana dashboards.

Label values must be low-cardinality and free of personal data: use route templates
(``/api/v1/accounts/{account_id}``), never raw paths, user ids or account numbers.
"""

from prometheus_client import Counter, Histogram

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
