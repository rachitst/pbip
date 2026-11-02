"""Response and request models of the compliance exceptions dashboard (TASK-100)."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from pbip_api.core.validation import Text
from pbip_api.modules.compliance.exceptions import (
    ACKNOWLEDGE_SLA,
    REVIEW_SLA,
    ExceptionKind,
    ExceptionRecord,
    ExceptionStatus,
    ExceptionsView,
    Step,
)

MAX_REASON_CHARS = 500


class ExceptionActionRequest(BaseModel):
    """Why the officer acknowledges or resolves; stored in the audit log with the actor."""

    model_config = ConfigDict(extra="forbid")

    reason: Annotated[Text, StringConstraints(strip_whitespace=True)] = Field(
        min_length=3, max_length=MAX_REASON_CHARS
    )


class ExceptionStepOut(BaseModel):
    by_user_id: UUID | None = Field(description="Staff user who took the step")
    at: datetime
    reason: str


class ExceptionOut(BaseModel):
    id: str = Field(description="KIND:reference; stable, used in the action URLs")
    kind: ExceptionKind
    ref: str = Field(
        description="Fraud event id, security event id, or first failing audit sequence number"
    )
    occurred_at: datetime
    status: ExceptionStatus
    acknowledged: ExceptionStepOut | None
    resolved: ExceptionStepOut | None
    risk_score: int | None = Field(default=None, description="Blocked payments only")
    amount_minor: int | None = Field(default=None, description="Blocked payments only")
    currency: str | None = None
    transaction_type: str | None = None
    reason_codes: list[str] = Field(
        default_factory=list, description="Fraud rule/model codes of a blocked payment"
    )
    first_invalid_seq: int | None = Field(default=None, description="Audit chain failures only")


class ExceptionCounts(BaseModel):
    OPEN: int
    ACKNOWLEDGED: int
    RESOLVED: int


class ExceptionsByKind(BaseModel):
    BLOCKED_TRANSFER: ExceptionCounts
    ACCOUNT_LOCKOUT: ExceptionCounts
    AUDIT_CHAIN_FAILURE: ExceptionCounts


class AuditChainOut(BaseModel):
    valid: bool
    checked: int
    first_invalid_seq: int | None


class RegulatoryIndicators(BaseModel):
    window_days: int = Field(description="Exceptions that occurred in the last N days are counted")
    review_sla_hours: int
    acknowledge_sla_hours: int
    unresolved_exceptions: int = Field(description="OPEN + ACKNOWLEDGED exceptions in the window")
    unacknowledged_past_sla: int = Field(
        description="OPEN exceptions that occurred more than acknowledge_sla_hours ago"
    )
    reviews_pending: int = Field(description="Fraud alerts still OPEN (held payments), any age")
    reviews_past_sla: int = Field(
        description="Fraud alerts still OPEN that were raised more than review_sla_hours ago"
    )
    oldest_review_at: datetime | None
    by_kind: ExceptionsByKind


class ComplianceExceptionsOut(BaseModel):
    generated_at: datetime
    indicators: RegulatoryIndicators
    audit_chain: AuditChainOut
    items: list[ExceptionOut] = Field(description="Matching exceptions, newest first")


def _step(step: Step | None) -> ExceptionStepOut | None:
    if step is None:
        return None
    return ExceptionStepOut(by_user_id=step.by_user_id, at=step.at, reason=step.reason)


def exception_out(record: ExceptionRecord) -> ExceptionOut:
    detail = record.detail
    return ExceptionOut(
        id=record.id,
        kind=record.kind,
        ref=record.ref,
        occurred_at=record.occurred_at,
        status=record.status,
        acknowledged=_step(record.acknowledged),
        resolved=_step(record.resolved),
        risk_score=detail.get("risk_score"),
        amount_minor=detail.get("amount_minor"),
        currency=detail.get("currency"),
        transaction_type=detail.get("transaction_type"),
        reason_codes=list(detail.get("reason_codes", [])),
        first_invalid_seq=detail.get("first_invalid_seq"),
    )


def dashboard_out(view: ExceptionsView, now: datetime) -> ComplianceExceptionsOut:
    unresolved = sum(
        c[ExceptionStatus.OPEN] + c[ExceptionStatus.ACKNOWLEDGED] for c in view.counts.values()
    )
    return ComplianceExceptionsOut(
        generated_at=now,
        indicators=RegulatoryIndicators(
            window_days=view.window_days,
            review_sla_hours=int(REVIEW_SLA.total_seconds() // 3600),
            acknowledge_sla_hours=int(ACKNOWLEDGE_SLA.total_seconds() // 3600),
            unresolved_exceptions=unresolved,
            unacknowledged_past_sla=view.unacknowledged_past_sla,
            reviews_pending=view.reviews_pending,
            reviews_past_sla=view.reviews_past_sla,
            oldest_review_at=view.oldest_review_at,
            by_kind=ExceptionsByKind(
                **{
                    kind.value: ExceptionCounts(**{s.value: n for s, n in counts.items()})
                    for kind, counts in view.counts.items()
                }
            ),
        ),
        audit_chain=AuditChainOut(
            valid=view.chain_valid,
            checked=view.chain_checked,
            first_invalid_seq=view.chain_first_invalid_seq,
        ),
        items=[exception_out(r) for r in view.items],
    )
