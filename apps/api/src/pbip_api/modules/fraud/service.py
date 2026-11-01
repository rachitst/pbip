"""Persistence of fraud decisions and the analyst alert queue."""

from collections import Counter
from datetime import datetime, timedelta
from uuid import UUID

from pbip_ml.fraud.contract import FraudScoreResult
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from pbip_api.core.db import utcnow
from pbip_api.core.errors import NotFoundError
from pbip_api.modules.fraud.models import AlertSeverity, AlertStatus, FraudAlert, FraudEvent
from pbip_api.modules.fraud.schemas import (
    DailyDecisionPoint,
    DecisionSummary,
    FraudStatsResponse,
    HighRiskPayeeStat,
    TriggeredRuleStat,
)
from pbip_api.modules.ledger.models import Transaction
from pbip_api.modules.payees.models import Payee

_HIGH_SEVERITY_SCORE = 85


def record_event(
    session: Session, transaction_id: UUID, customer_id: UUID, result: FraudScoreResult
) -> FraudEvent:
    event = FraudEvent(
        transaction_id=transaction_id,
        customer_id=customer_id,
        risk_score=result.risk_score,
        decision=result.decision.value,
        reasons=[reason.model_dump(mode="json") for reason in result.reasons],
        features=dict(result.features),
        model_version=result.model_version,
        latency_ms=result.latency_ms,
    )
    session.add(event)
    session.flush()
    return event


def open_alert(session: Session, event: FraudEvent) -> FraudAlert:
    alert = FraudAlert(
        fraud_event_id=event.id,
        transaction_id=event.transaction_id,
        severity=AlertSeverity.HIGH
        if event.risk_score >= _HIGH_SEVERITY_SCORE
        else AlertSeverity.MEDIUM,
    )
    session.add(alert)
    session.flush()
    return alert


def list_alerts(
    session: Session, status: AlertStatus | None, limit: int, offset: int
) -> list[tuple[FraudAlert, FraudEvent, Transaction]]:
    stmt = (
        select(FraudAlert, FraudEvent, Transaction)
        .join(FraudEvent, FraudEvent.id == FraudAlert.fraud_event_id)
        .join(Transaction, Transaction.id == FraudAlert.transaction_id)
        .order_by(FraudAlert.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if status is not None:
        stmt = stmt.where(FraudAlert.status == status)
    return [(a, e, t) for a, e, t in session.execute(stmt).all()]


def get_alert(session: Session, alert_id: UUID) -> tuple[FraudAlert, FraudEvent, Transaction]:
    row = session.execute(
        select(FraudAlert, FraudEvent, Transaction)
        .join(FraudEvent, FraudEvent.id == FraudAlert.fraud_event_id)
        .join(Transaction, Transaction.id == FraudAlert.transaction_id)
        .where(FraudAlert.id == alert_id)
    ).first()
    if row is None:
        raise NotFoundError("alert not found")
    return row[0], row[1], row[2]


def _get_decision_counts_and_rates(
    session: Session, cutoff: datetime
) -> tuple[dict[str, int], DecisionSummary]:
    stmt = (
        select(FraudEvent.decision, func.count(FraudEvent.id))
        .where(FraudEvent.created_at >= cutoff)
        .group_by(FraudEvent.decision)
    )
    decision_map: dict[str, int] = {
        "ALLOW": 0,
        "STEP_UP": 0,
        "REVIEW": 0,
        "BLOCK": 0,
    }
    for dec, cnt in session.execute(stmt).all():
        decision_map[str(dec)] = int(cnt)

    allow = decision_map.get("ALLOW", 0)
    step_up = decision_map.get("STEP_UP", 0)
    review = decision_map.get("REVIEW", 0)
    block = decision_map.get("BLOCK", 0)
    total = allow + step_up + review + block

    hold_rate = round(review / total, 4) if total > 0 else 0.0
    block_rate = round(block / total, 4) if total > 0 else 0.0
    step_up_rate = round(step_up / total, 4) if total > 0 else 0.0

    summary = DecisionSummary(
        allow=allow,
        step_up=step_up,
        review=review,
        block=block,
        total=total,
        hold_rate=hold_rate,
        block_rate=block_rate,
        step_up_rate=step_up_rate,
    )
    return decision_map, summary


def _get_daily_timeline(session: Session, cutoff: datetime) -> list[DailyDecisionPoint]:
    day_col = func.date_trunc("day", FraudEvent.created_at).label("day")
    stmt = (
        select(day_col, FraudEvent.decision, func.count(FraudEvent.id))
        .where(FraudEvent.created_at >= cutoff)
        .group_by(day_col, FraudEvent.decision)
        .order_by(day_col.asc())
    )
    daily_points: dict[str, dict[str, int]] = {}
    for day_val, dec, cnt in session.execute(stmt).all():
        if isinstance(day_val, datetime):
            day_str = day_val.strftime("%Y-%m-%d")
        else:
            day_str = str(day_val)[:10]
        if day_str not in daily_points:
            daily_points[day_str] = {"ALLOW": 0, "STEP_UP": 0, "REVIEW": 0, "BLOCK": 0}
        daily_points[day_str][str(dec)] = int(cnt)

    return [
        DailyDecisionPoint(
            date=d,
            allow=counts.get("ALLOW", 0),
            step_up=counts.get("STEP_UP", 0),
            review=counts.get("REVIEW", 0),
            block=counts.get("BLOCK", 0),
            total=sum(counts.values()),
        )
        for d, counts in sorted(daily_points.items())
    ]


def _get_rules_breakdown(
    session: Session, cutoff: datetime, total_decisions: int
) -> list[TriggeredRuleStat]:
    stmt = select(FraudEvent.reasons).where(
        FraudEvent.created_at >= cutoff,
    )
    rule_counts: Counter[str] = Counter()
    for (reasons,) in session.execute(stmt).all():
        if isinstance(reasons, list):
            for reason in reasons:
                if isinstance(reason, dict) and "code" in reason:
                    code = str(reason["code"])
                    rule_counts[code] += 1

    stats: list[TriggeredRuleStat] = []
    for code, count in rule_counts.most_common(10):
        pct = round((count / total_decisions) * 100, 2) if total_decisions > 0 else 0.0
        stats.append(TriggeredRuleStat(rule_code=code, count=count, percentage=pct))
    return stats


def _get_high_risk_payees(session: Session, cutoff: datetime) -> list[HighRiskPayeeStat]:
    flagged_expr = case(
        (FraudEvent.decision.in_(["REVIEW", "BLOCK", "STEP_UP"]), 1),
        else_=None,
    )
    stmt = (
        select(
            Payee.id,
            Payee.name,
            Payee.bank_code,
            func.count(flagged_expr).label("flagged_txns"),
            func.avg(FraudEvent.risk_score).label("avg_risk_score"),
        )
        .join(Transaction, Transaction.id == FraudEvent.transaction_id)
        .join(Payee, Payee.id == Transaction.payee_id)
        .where(FraudEvent.created_at >= cutoff)
        .group_by(Payee.id, Payee.name, Payee.bank_code)
        .order_by(
            func.count(flagged_expr).desc(),
            func.avg(FraudEvent.risk_score).desc(),
        )
        .limit(10)
    )
    results: list[HighRiskPayeeStat] = []
    for row in session.execute(stmt).all():
        flagged = int(row.flagged_txns or 0)
        avg_score = round(float(row.avg_risk_score or 0.0), 1)
        if flagged > 0 or avg_score >= 50.0:
            results.append(
                HighRiskPayeeStat(
                    payee_id=row.id,
                    payee_name=row.name,
                    bank_code=row.bank_code,
                    flagged_count=flagged,
                    avg_risk_score=avg_score,
                )
            )
    return results


def get_fraud_stats(
    session: Session,
    days: int = 30,
    now: datetime | None = None,
) -> FraudStatsResponse:
    """Compute fraud decision telemetry, rule breakdown, and high-risk payees."""
    if now is None:
        now = utcnow()
    cutoff = now - timedelta(days=days)

    decision_counts, summary = _get_decision_counts_and_rates(session, cutoff)
    timeline = _get_daily_timeline(session, cutoff)
    rules_breakdown = _get_rules_breakdown(session, cutoff, summary.total)
    high_risk_payees = _get_high_risk_payees(session, cutoff)

    return FraudStatsResponse(
        period_days=days,
        total_decisions=summary.total,
        decision_counts=decision_counts,
        hold_rate=summary.hold_rate,
        block_rate=summary.block_rate,
        summary=summary,
        timeline=timeline,
        rules_breakdown=rules_breakdown,
        high_risk_payees=high_risk_payees,
    )
