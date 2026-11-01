"""Fraud telemetry for analysts (TASK-035): decision trends, hold rate, rule triggers, risky payees.

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
from sqlalchemy import ColumnElement, Date, cast, func, literal_column, select
from sqlalchemy.orm import Session

from pbip_api.modules.accounts.service import mask_account_number
from pbip_api.modules.fraud.models import FraudEvent
from pbip_api.modules.ledger.models import Transaction
from pbip_api.modules.payees.models import Payee

_logger = structlog.get_logger(__name__)

CACHE_TTL_SECONDS = 60
DECISIONS = ("ALLOW", "STEP_UP", "REVIEW", "BLOCK")
TOP_N = 10
RISKY_AT = 60  # REVIEW and above


def _cache_key(days: int) -> str:
    return f"fraud:stats:v1:{days}"


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
    in_window = FraudEvent.created_at >= since
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
    }


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
            FraudEvent.created_at >= since
        )
    )
    return None if value is None else round(float(value), 2)
