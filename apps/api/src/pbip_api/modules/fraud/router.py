from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from redis import Redis
from sqlalchemy.orm import Session

from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import get_session, utcnow
from pbip_api.core.redis import get_redis
from pbip_api.core.validation import Text
from pbip_api.modules.accounts.service import mask_account_number
from pbip_api.modules.auth.dependencies import (
    Principal,
    get_clock,
    get_request_meta,
    require_roles,
)
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.auth.service import RequestMeta
from pbip_api.modules.bills.service import settle_reviewed_bill
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.fraud import monitoring, service, stats
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


class HighRiskCustomer(BaseModel):
    customer_id: UUID
    flagged_events: int = Field(
        description="REVIEW/BLOCK decisions, or decisions that opened an alert"
    )
    blocked: int
    max_risk: int
    open_alerts: int
    last_flagged_at: datetime
    alert_id: UUID | None = Field(description="Alert to open: newest open one, else newest")


class OldestOpenAlert(BaseModel):
    alert_id: UUID
    severity: str
    risk_score: int
    created_at: datetime


class OpenAlertsPanel(BaseModel):
    total: int
    by_severity: dict[str, int] = Field(description="Open alerts per severity (MEDIUM, HIGH)")
    oldest_open_at: datetime | None
    oldest_open_age_seconds: int | None = Field(description="Age of the oldest open alert")
    oldest: list[OldestOpenAlert] = Field(description="The five longest-waiting open alerts")


class LoginBurst(BaseModel):
    user_id: UUID
    failures: int
    last_failure_at: datetime


class Lockout(BaseModel):
    user_id: UUID | None = Field(description="None if the user was deleted since")
    locked_at: datetime


class LoginAnomalies(BaseModel):
    window_hours: int
    burst_threshold: int = Field(description="Failed sign-ins in the window that make a burst")
    counts: dict[str, int] = Field(description="Security events in the window, by type")
    unknown_account_failures: int = Field(description="Failed sign-ins for unknown emails")
    bursts: list[LoginBurst]
    recent_lockouts: list[Lockout] = Field(description="The ten newest ACCOUNT_LOCKED events")


class FlaggedEvent(BaseModel):
    event_id: UUID
    created_at: datetime
    decision: str
    risk_score: int
    customer_id: UUID
    alert_id: UUID | None
    alert_status: str | None
    amount_minor: int
    currency: str
    reason_codes: list[str]


class FraudEventsPanel(BaseModel):
    last_24h: dict[str, int] = Field(description="REVIEW and BLOCK decisions in 24 hours")
    latest: list[FlaggedEvent] = Field(description="The ten newest flagged decisions")


class SecurityMonitoring(BaseModel):
    """UC-13 panels. Identifiers and aggregates only: no names, emails, account numbers or IPs."""

    generated_at: datetime
    high_risk_window_days: int
    high_risk_customers: list[HighRiskCustomer]
    open_alerts: OpenAlertsPanel
    login_anomalies: LoginAnomalies
    fraud_events: FraudEventsPanel


class DemoLabStatus(BaseModel):
    enabled: bool = Field(
        description="False in production: the staff fraud demonstration lab must not run there"
    )


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


@router.get(
    "/monitoring",
    response_model=SecurityMonitoring,
    summary="Security monitoring: high-risk customers, open alerts, login anomalies, fraud events",
)
def get_monitoring(
    _analyst: Principal = Depends(analyst_gate),
    session: Session = Depends(get_session),
    redis_client: Redis = Depends(get_redis),
    clock: Callable[[], float] = Depends(get_clock),
) -> SecurityMonitoring:
    """UC-13 dashboard data (TASK-099), cached in Redis for 60 seconds."""
    now = datetime.fromtimestamp(clock(), tz=UTC)
    return SecurityMonitoring.model_validate(
        monitoring.security_monitoring(session, redis_client, now)
    )


@router.get(
    "/demo-lab",
    response_model=DemoLabStatus,
    summary="Whether the fraud demonstration lab (TASK-081) may run in this environment",
)
def get_demo_lab_status(
    _analyst: Principal = Depends(analyst_gate),
    settings: Settings = Depends(get_settings),
) -> DemoLabStatus:
    """The lab drives real customer APIs with demo credentials. The web app only knows how it was
    built, not where it is deployed, so the server's APP_ENV decides; production says no."""
    return DemoLabStatus(enabled=settings.app_env != "production")


@router.get("/alerts", response_model=list[AlertSummary], summary="Alert queue (newest first)")
def list_alerts(
    alert_status: AlertStatus | None = Query(default=None, alias="status"),
    transaction_id: UUID | None = Query(
        default=None, description="Only the alert raised for this transaction (at most one)"
    ),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
    _analyst: Principal = Depends(analyst_gate),
    session: Session = Depends(get_session),
) -> list[AlertSummary]:
    rows = service.list_alerts(session, alert_status, limit, offset, transaction_id)
    return [_summary(*row) for row in rows]


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
        settle_other=settle_reviewed_bill,  # a held bill payment follows the decision too
    )


# mask helper re-exported for the account numbers shown in future alert views
__all__ = ["mask_account_number", "router"]
