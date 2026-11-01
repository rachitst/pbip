"""Security monitoring for analysts (TASK-099, UC-13): one view of threats across customers.

Four panels, all read-only aggregates over tables the platform already writes:

* high-risk customers: who had the most flagged fraud decisions in the last 30 days;
* open alerts (open investigations): counts by severity and the oldest still waiting;
* login anomalies: users with a burst of failed sign-ins in 24 hours, and recent lockouts;
* recent flagged fraud events, each with the alert an analyst can open.

A decision is "flagged" when it was REVIEW or BLOCK, or when it opened an alert later (a STEP_UP
the customer could not complete is escalated to review with an alert on the original event).

Identifiers only: customer and user ids, alert ids, rule codes, amounts. No names, emails, account
numbers or IP addresses: the analyst opens the alert for the customer details the role may see.
The result is cached in Redis for 60 seconds like ``/fraud/stats``, so a dashboard that polls
cannot hammer PostgreSQL; if Redis is down the panels are computed directly (fail-open).
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from pbip_api.modules.auth.models import SecurityEvent, SecurityEventType
from pbip_api.modules.fraud.models import AlertSeverity, AlertStatus, FraudAlert, FraudEvent
from pbip_api.modules.ledger.models import Transaction

_logger = structlog.get_logger(__name__)

CACHE_KEY = "fraud:monitoring:v1"
CACHE_TTL_SECONDS = 60
HIGH_RISK_WINDOW = timedelta(days=30)
RECENT_WINDOW = timedelta(hours=24)
BURST_THRESHOLD = 3  # failed sign-ins for one user in RECENT_WINDOW
TOP_N = 10
OLDEST_OPEN_N = 5
FLAGGED_DECISIONS = ("REVIEW", "BLOCK")
# Security events counted in the login panel, in the order the UI lists them.
COUNTED_EVENTS = (
    SecurityEventType.LOGIN_FAILURE,
    SecurityEventType.ACCOUNT_LOCKED,
    SecurityEventType.LOGIN_BLOCKED_LOCKED,
    SecurityEventType.MFA_FAILURE,
    SecurityEventType.STEP_UP_FAILURE,
)


def security_monitoring(session: Session, redis_client: Redis, now: datetime) -> dict[str, Any]:
    """All four panels as of ``now``, from the 60 s cache when possible."""
    try:
        cached = redis_client.get(CACHE_KEY)
    except RedisError as exc:
        _logger.warning("security_monitoring_cache_unavailable", error_type=type(exc).__name__)
        cached = None
    if cached:
        return dict(json.loads(str(cached)))
    panels = compute(session, now)
    try:
        redis_client.set(CACHE_KEY, json.dumps(panels), ex=CACHE_TTL_SECONDS)
    except RedisError as exc:
        _logger.warning("security_monitoring_cache_unavailable", error_type=type(exc).__name__)
    return panels


def compute(session: Session, now: datetime) -> dict[str, Any]:
    return {
        "generated_at": _iso(now),
        "high_risk_window_days": HIGH_RISK_WINDOW.days,
        "high_risk_customers": _high_risk_customers(session, now - HIGH_RISK_WINDOW),
        "open_alerts": _open_alerts(session, now),
        "login_anomalies": _login_anomalies(session, now - RECENT_WINDOW),
        "fraud_events": _fraud_events(session, now - RECENT_WINDOW, now - HIGH_RISK_WINDOW),
    }


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _flagged() -> ColumnElement[bool]:
    """REVIEW/BLOCK, or any decision that opened an alert (needs the outer join to alerts)."""
    return or_(FraudEvent.decision.in_(FLAGGED_DECISIONS), FraudAlert.id.is_not(None))


def _high_risk_customers(session: Session, since: datetime) -> list[dict[str, Any]]:
    """Customers with the most flagged decisions, then the highest score, in the window."""
    flagged = func.count()
    max_risk = func.max(FraudEvent.risk_score)
    rows = session.execute(
        select(
            FraudEvent.customer_id,
            flagged,
            func.count().filter(FraudEvent.decision == "BLOCK"),
            max_risk,
            func.count(FraudAlert.id).filter(FraudAlert.status == AlertStatus.OPEN),
            func.max(FraudEvent.created_at),
        )
        .outerjoin(FraudAlert, FraudAlert.fraud_event_id == FraudEvent.id)
        .where(FraudEvent.created_at >= since, _flagged())
        .group_by(FraudEvent.customer_id)
        .order_by(flagged.desc(), max_risk.desc(), FraudEvent.customer_id)
        .limit(TOP_N)
    ).all()
    alerts = _alert_to_open(session, [row[0] for row in rows], since)
    return [
        {
            "customer_id": str(customer_id),
            "flagged_events": int(n),
            "blocked": int(blocked),
            "max_risk": int(risk),
            "open_alerts": int(open_alerts),
            "last_flagged_at": _iso(last),
            "alert_id": alerts.get(customer_id),
        }
        for customer_id, n, blocked, risk, open_alerts, last in rows
    ]


def _alert_to_open(session: Session, customer_ids: list[UUID], since: datetime) -> dict[UUID, str]:
    """Per customer, the alert a drill-down should open: the newest OPEN one, else the newest."""
    if not customer_ids:
        return {}
    rows = session.execute(
        select(FraudEvent.customer_id, FraudAlert.id)
        .join(FraudAlert, FraudAlert.fraud_event_id == FraudEvent.id)
        .where(FraudEvent.customer_id.in_(customer_ids), FraudEvent.created_at >= since)
        .order_by(
            FraudEvent.customer_id,
            (FraudAlert.status == AlertStatus.OPEN).desc(),
            FraudAlert.created_at.desc(),
        )
        .ext(distinct_on(FraudEvent.customer_id))
    ).all()
    return {customer_id: str(alert_id) for customer_id, alert_id in rows}


def _open_alerts(session: Session, now: datetime) -> dict[str, Any]:
    """Open investigations: how many, how severe, and how long the oldest has waited."""
    is_open = FraudAlert.status == AlertStatus.OPEN
    by_severity = dict.fromkeys((s.value for s in AlertSeverity), 0)
    for severity, count in session.execute(
        select(FraudAlert.severity, func.count()).where(is_open).group_by(FraudAlert.severity)
    ):
        by_severity[AlertSeverity(severity).value] = int(count)
    oldest = session.execute(
        select(FraudAlert.id, FraudAlert.severity, FraudAlert.created_at, FraudEvent.risk_score)
        .join(FraudEvent, FraudEvent.id == FraudAlert.fraud_event_id)
        .where(is_open)
        .order_by(FraudAlert.created_at, FraudAlert.id)
        .limit(OLDEST_OPEN_N)
    ).all()
    oldest_at = oldest[0][2] if oldest else None
    return {
        "total": sum(by_severity.values()),
        "by_severity": by_severity,
        "oldest_open_at": _iso(oldest_at) if oldest_at else None,
        "oldest_open_age_seconds": max(0, int((now - oldest_at).total_seconds()))
        if oldest_at
        else None,
        "oldest": [
            {
                "alert_id": str(alert_id),
                "severity": AlertSeverity(severity).value,
                "risk_score": int(risk),
                "created_at": _iso(created_at),
            }
            for alert_id, severity, created_at, risk in oldest
        ],
    }


def _login_anomalies(session: Session, since: datetime) -> dict[str, Any]:
    """Failed sign-in bursts per user and lockouts; user ids and times only, never IPs."""
    in_window = SecurityEvent.created_at >= since
    counts = dict.fromkeys((e.value for e in COUNTED_EVENTS), 0)
    for event_type, count in session.execute(
        select(SecurityEvent.event_type, func.count())
        .where(in_window, SecurityEvent.event_type.in_(COUNTED_EVENTS))
        .group_by(SecurityEvent.event_type)
    ):
        counts[SecurityEventType(event_type).value] = int(count)
    is_failure = SecurityEvent.event_type == SecurityEventType.LOGIN_FAILURE
    unknown = session.scalar(
        select(func.count()).where(in_window, is_failure, SecurityEvent.user_id.is_(None))
    )
    failures = func.count()
    bursts = session.execute(
        select(SecurityEvent.user_id, failures, func.max(SecurityEvent.created_at))
        .where(in_window, is_failure, SecurityEvent.user_id.is_not(None))
        .group_by(SecurityEvent.user_id)
        .having(failures >= BURST_THRESHOLD)
        .order_by(failures.desc(), func.max(SecurityEvent.created_at).desc())
        .limit(TOP_N)
    ).all()
    lockouts = session.execute(
        select(SecurityEvent.user_id, SecurityEvent.created_at)
        .where(SecurityEvent.event_type == SecurityEventType.ACCOUNT_LOCKED)
        .order_by(SecurityEvent.created_at.desc(), SecurityEvent.id)
        .limit(TOP_N)
    ).all()
    return {
        "window_hours": int(RECENT_WINDOW.total_seconds() // 3600),
        "burst_threshold": BURST_THRESHOLD,
        "counts": counts,
        "unknown_account_failures": int(unknown or 0),
        "bursts": [
            {"user_id": str(user_id), "failures": int(n), "last_failure_at": _iso(last)}
            for user_id, n, last in bursts
        ],
        "recent_lockouts": [
            {"user_id": str(user_id) if user_id else None, "locked_at": _iso(at)}
            for user_id, at in lockouts
        ],
    }


def _fraud_events(session: Session, since: datetime, horizon: datetime) -> dict[str, Any]:
    """The newest flagged decisions with the alert to open, and the 24-hour counts."""
    last_24h = dict.fromkeys(FLAGGED_DECISIONS, 0)
    for decision, count in session.execute(
        select(FraudEvent.decision, func.count())
        .where(FraudEvent.created_at >= since, FraudEvent.decision.in_(FLAGGED_DECISIONS))
        .group_by(FraudEvent.decision)
    ):
        last_24h[decision] = int(count)
    rows = session.execute(
        select(
            FraudEvent, FraudAlert.id, FraudAlert.status, Transaction.amount, Transaction.currency
        )
        .join(Transaction, Transaction.id == FraudEvent.transaction_id)
        .outerjoin(FraudAlert, FraudAlert.fraud_event_id == FraudEvent.id)
        .where(FraudEvent.created_at >= horizon, _flagged())
        .order_by(FraudEvent.created_at.desc(), FraudEvent.id)
        .limit(TOP_N)
    ).all()
    return {
        "last_24h": last_24h,
        "latest": [
            {
                "event_id": str(event.id),
                "created_at": _iso(event.created_at),
                "decision": event.decision,
                "risk_score": event.risk_score,
                "customer_id": str(event.customer_id),
                "alert_id": str(alert_id) if alert_id else None,
                "alert_status": AlertStatus(status).value if status else None,
                "amount_minor": int(amount),
                "currency": currency,
                "reason_codes": _codes(event.reasons),
            }
            for event, alert_id, status, amount, currency in rows
        ],
    }


def _codes(reasons: list[dict[str, Any]]) -> list[str]:
    """Rule and model codes only: messages may quote amounts or payee timing, codes never do."""
    return [
        str(r["code"]) for r in reasons if isinstance(r, dict) and isinstance(r.get("code"), str)
    ]
