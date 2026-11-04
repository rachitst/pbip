"""Fraud telemetry for analysts (TASK-035): decision trends, hold rate, rule triggers, risky payees,
the risk-score distribution and the alert outcomes the fraud dashboard charts.

Aggregates only: counts, rates, rule codes and MASKED destination accounts. No customer names, no
full account numbers. Results are cached in Redis for 60 seconds per window, so a dashboard that
polls cannot hammer PostgreSQL; if Redis is down the numbers are computed directly (fail-open).
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import ColumnElement, Date, and_, cast, func, literal_column, select
from sqlalchemy.orm import Session

from pbip_api.modules.accounts.service import mask_account_number
from pbip_api.modules.fraud.models import AlertStatus, FraudAlert, FraudEvent
from pbip_api.modules.fraud.structuring_monitor import MONITOR_VERSION
from pbip_api.modules.ledger.models import Transaction
from pbip_api.modules.payees.models import Payee

_logger = structlog.get_logger(__name__)

CACHE_TTL_SECONDS = 60
DECISIONS = ("ALLOW", "STEP_UP", "REVIEW", "BLOCK")
TOP_N = 10
RISKY_AT = 60  # REVIEW and above
BAND_WIDTH = 10  # risk-score histogram: 0-9, 10-19, ..., 90-100 (100 joins the top band)
BANDS = 10


def _cache_key(days: int) -> str:
    # v2 added risk_bands and alerts: a v1 payload cached before a deploy would fail validation.
    return f"fraud:stats:v2:{days}"


def fraud_stats(session: Session, redis_client: Redis, days: int, now: datetime) -> dict[str, Any]:
    """The stats for the last ``days`` days, from the 60 s cache when possible."""
    key = _cache_key(days)
    try:
        cached = redis_client.get(key)
    except RedisError as exc:
        _logger.warning("fraud_stats_cache_unavailable", error_type=type(exc).__name__)
        cached = None
    if cached:
        return dict(json.loads(str(cached)))
    stats = compute(session, days, now)
    try:
        redis_client.set(key, json.dumps(stats), ex=CACHE_TTL_SECONDS)
    except RedisError as exc:
        _logger.warning("fraud_stats_cache_unavailable", error_type=type(exc).__name__)
    return stats


def compute(session: Session, days: int, now: datetime) -> dict[str, Any]:
    since = now - timedelta(days=days)
    # Payment decisions only: a batch monitor's alert (ADR-0052) did not score a payment.
    in_window = and_(FraudEvent.created_at >= since, FraudEvent.model_version != MONITOR_VERSION)
    day = cast(func.timezone("UTC", FraudEvent.created_at), Date)
    per_day: dict[str, dict[str, int]] = {}
    for d, decision, count in session.execute(
        select(day, FraudEvent.decision, func.count())
        .where(in_window)
        .group_by(day, FraudEvent.decision)
        .order_by(day)
    ):
        per_day.setdefault(d.isoformat(), dict.fromkeys(DECISIONS, 0))[decision] = int(count)
    totals = {d: sum(v[d] for v in per_day.values()) for d in DECISIONS}
    scored = sum(totals.values())
    return {
        "window_days": days,
        "generated_at": now.astimezone(UTC).isoformat(),
        "scored": scored,
        "totals": totals,
        "hold_rate": round(totals["REVIEW"] / scored, 4) if scored else 0.0,
        "block_rate": round(totals["BLOCK"] / scored, 4) if scored else 0.0,
        "by_day": [{"day": d, **counts} for d, counts in per_day.items()],
        "top_rules": _top_rules(session, since),
        "risky_payees": _risky_payees(session, since),
        "model_fallbacks": int(
            session.scalar(
                select(func.count()).where(in_window, FraudEvent.model_version.like("%+rules-only"))
            )
            or 0
        ),
        "latency_p95_ms": _p95(session, since),
        "risk_bands": _risk_bands(session, in_window),
        "alerts": _alerts(session, since),
    }


def _risk_bands(session: Session, in_window: ColumnElement[bool]) -> list[dict[str, int]]:
    """How many payment decisions fell in each 10-point risk band; every band, zeros included."""
    band = func.least(FraudEvent.risk_score // BAND_WIDTH, BANDS - 1)
    counts = {
        int(b): int(n)
        for b, n in session.execute(select(band, func.count()).where(in_window).group_by(band))
    }
    return [
        {
            "low": i * BAND_WIDTH,
            "high": 100 if i == BANDS - 1 else (i + 1) * BAND_WIDTH - 1,
            "count": counts.get(i, 0),
        }
        for i in range(BANDS)
    ]


def _alerts(session: Session, since: datetime) -> dict[str, int]:
    """Alerts raised in the window by where they stand now: open, confirmed fraud or released."""
    counts = dict.fromkeys((s.value for s in AlertStatus), 0)
    for status, n in session.execute(
        select(FraudAlert.status, func.count())
        .where(FraudAlert.created_at >= since)
        .group_by(FraudAlert.status)
    ):
        counts[AlertStatus(status).value] = int(n)
    return counts


def _top_rules(session: Session, since: datetime) -> list[dict[str, Any]]:
    """How often each reason code fired (RULE, MODEL and ANOMALY reasons alike)."""
    reason = func.jsonb_array_elements(FraudEvent.reasons).table_valued("value").alias("reason")
    code: ColumnElement[str] = literal_column("reason.value ->> 'code'")
    source: ColumnElement[str] = literal_column("reason.value ->> 'source'")
    rows = session.execute(
        select(code, source, func.count())
        .select_from(FraudEvent)
        .join(reason, literal_column("true"))
        .where(FraudEvent.created_at >= since)
        .group_by(code, source)
        .order_by(func.count().desc(), code)
        .limit(TOP_N)
    ).all()
    return [{"code": c, "source": s, "count": int(n)} for c, s, n in rows]


def _risky_payees(session: Session, since: datetime) -> list[dict[str, Any]]:
    """Destination accounts behind REVIEW/BLOCK decisions, across customers (mule signal)."""
    rows = session.execute(
        select(
            Payee.bank_code,
            Payee.account_number,
            func.count(),
            func.count(func.distinct(FraudEvent.customer_id)),
            func.max(FraudEvent.risk_score),
        )
        .join(Transaction, Transaction.id == FraudEvent.transaction_id)
        .join(Payee, Payee.id == Transaction.payee_id)
        .where(FraudEvent.created_at >= since, FraudEvent.risk_score >= RISKY_AT)
        .group_by(Payee.bank_code, Payee.account_number)
        .order_by(func.count().desc(), func.max(FraudEvent.risk_score).desc())
        .limit(TOP_N)
    ).all()
    return [
        {
            "bank_code": bank,
            "account": mask_account_number(number),
            "flagged_transfers": int(n),
            "customers": int(customers),
            "max_risk": int(max_risk),
        }
        for bank, number, n, customers, max_risk in rows
    ]


def _p95(session: Session, since: datetime) -> float | None:
    value = session.scalar(
        select(func.percentile_cont(0.95).within_group(FraudEvent.latency_ms)).where(
            FraudEvent.created_at >= since, FraudEvent.model_version != MONITOR_VERSION
        )
    )
    return None if value is None else round(float(value), 2)
