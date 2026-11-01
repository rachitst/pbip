from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from redis import Redis
from sqlalchemy.orm import Session

from pbip_api.core.db import get_session, utcnow
from pbip_api.core.redis import get_redis
from pbip_api.core.validation import Text
from pbip_api.modules.accounts.service import mask_account_number
from pbip_api.modules.auth.dependencies import Principal, get_request_meta, require_roles
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.fraud import service, stats
from pbip_api.modules.fraud.models import AlertStatus, FraudAlert, FraudEvent
from pbip_api.modules.ledger.models import Transaction
from pbip_api.modules.payments.router import get_payments_service
from pbip_api.modules.payments.service import PaymentsService

router = APIRouter(prefix="/api/v1/fraud", tags=["fraud"])

analyst_gate = require_roles(RoleName.FRAUD_ANALYST, RoleName.ADMIN, mfa=True)


class AlertSummary(BaseModel):
    id: UUID
    status: str
    severity: str
    risk_score: int
    decision: str
    amount_minor: int
    currency: str
    transaction_status: str
    created_at: datetime


class AlertCustomer(BaseModel):
    """What an analyst needs to judge the alert; no contact details or date of birth."""

    full_name: str
    kyc_status: str
    segment: str | None
    risk_profile: str | None
    customer_since: datetime


class AlertDetail(AlertSummary):
    transaction_id: UUID
    customer_id: UUID
    customer: AlertCustomer | None
    transaction_type: str
    reference: str | None
    reasons: list[dict[str, Any]]
    features: dict[str, Any]
    model_version: str
    latency_ms: float
    resolution_note: str | None
    resolved_at: datetime | None


class DayCounts(BaseModel):
    day: str = Field(description="UTC date, YYYY-MM-DD")
    ALLOW: int
    STEP_UP: int
    REVIEW: int
    BLOCK: int


class RuleCount(BaseModel):
    code: str
    source: str | None
    count: int


class RiskyPayee(BaseModel):
    bank_code: str
    account: str = Field(description="Masked: last four digits only")
    flagged_transfers: int
    customers: int = Field(description="Distinct customers who sent flagged money here")
    max_risk: int


class FraudStats(BaseModel):
    window_days: int
    generated_at: datetime
    scored: int
    totals: dict[str, int]
    hold_rate: float = Field(description="Share of scored transactions held for REVIEW")
    block_rate: float
    by_day: list[DayCounts]
    top_rules: list[RuleCount]
    risky_payees: list[RiskyPayee]
    model_fallbacks: int = Field(
        description="Decisions served by the rules because the model failed"
    )
    latency_p95_ms: float | None


class DecisionRequest(BaseModel):
    action: Literal["RELEASE", "REJECT"]
    note: Text | None = Field(default=None, max_length=1000)


def _summary(alert: FraudAlert, event: FraudEvent, txn: Transaction) -> AlertSummary:
    return AlertSummary(
        id=alert.id,
        status=alert.status.value,
        severity=alert.severity.value,
        risk_score=event.risk_score,
        decision=event.decision,
        amount_minor=txn.amount,
        currency=txn.currency,
        transaction_status=txn.status.value,
        created_at=alert.created_at,
    )


@router.get(
    "/stats",
    response_model=FraudStats,
    summary="Fraud telemetry: decisions over time, hold rate, top reasons, risky payees",
)
def get_stats(
    days: int = Query(default=7, ge=1, le=90),
    _analyst: Principal = Depends(analyst_gate),
    session: Session = Depends(get_session),
    redis_client: Redis = Depends(get_redis),
) -> FraudStats:
    """Aggregates only, cached in Redis for 60 seconds per window (TASK-035)."""
    return FraudStats.model_validate(stats.fraud_stats(session, redis_client, days, utcnow()))


@router.get("/alerts", response_model=list[AlertSummary], summary="Alert queue (newest first)")
def list_alerts(
    alert_status: AlertStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
    _analyst: Principal = Depends(analyst_gate),
    session: Session = Depends(get_session),
) -> list[AlertSummary]:
    return [_summary(*row) for row in service.list_alerts(session, alert_status, limit, offset)]


@router.get(
    "/alerts/{alert_id}",
    response_model=AlertDetail,
    summary="Why was this flagged? Score, reasons, features and model version",
)
def get_alert(
    alert_id: UUID,
    _analyst: Principal = Depends(analyst_gate),
    session: Session = Depends(get_session),
) -> AlertDetail:
    alert, event, txn = service.get_alert(session, alert_id)
    base = _summary(alert, event, txn)
    customer = session.get(Customer, event.customer_id)
    return AlertDetail(
        **base.model_dump(),
        transaction_id=txn.id,
        customer_id=event.customer_id,
        customer=AlertCustomer(
            full_name=customer.full_name,
            kyc_status=customer.kyc_status.value,
            segment=customer.segment,
            risk_profile=customer.risk_profile,
            customer_since=customer.created_at,
        )
        if customer is not None
        else None,
        transaction_type=txn.type.value,
        reference=txn.reference,
        reasons=event.reasons,
        features=dict(event.features),
        model_version=event.model_version,
        latency_ms=event.latency_ms,
        resolution_note=alert.resolution_note,
        resolved_at=alert.resolved_at,
    )


@router.post(
    "/alerts/{alert_id}/decision",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="RELEASE (false positive, posts the held transfer) or REJECT (confirmed fraud)",
)
def decide_alert(
    alert_id: UUID,
    body: DecisionRequest,
    analyst: Principal = Depends(analyst_gate),
    payments: PaymentsService = Depends(get_payments_service),
    meta: RequestMeta = Depends(get_request_meta),
) -> None:
    payments.review_alert(
        analyst_id=analyst.user_id,
        alert_id=alert_id,
        release=body.action == "RELEASE",
        note=body.note,
        meta=meta,
    )


# mask helper re-exported for the account numbers shown in future alert views
__all__ = ["mask_account_number", "router"]
