"""Persistence of fraud decisions and the analyst alert queue."""

from uuid import UUID

from pbip_ml.fraud.contract import FraudScoreResult
from sqlalchemy import select
from sqlalchemy.orm import Session

from pbip_api.core.errors import NotFoundError
from pbip_api.modules.fraud.models import AlertSeverity, AlertStatus, FraudAlert, FraudEvent
from pbip_api.modules.ledger.models import Transaction

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
    session: Session,
    status: AlertStatus | None,
    limit: int,
    offset: int,
    transaction_id: UUID | None = None,
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
    if transaction_id is not None:
        stmt = stmt.where(FraudAlert.transaction_id == transaction_id)
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
